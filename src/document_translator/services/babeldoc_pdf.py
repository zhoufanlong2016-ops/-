"""Controlled, high-level BabelDOC invocation for PDF translation.

The service treats BabelDOC's output as an untrusted candidate: a source
preflight is performed first, the cloud provider is reached only through the
local gateway, and the candidate is published atomically after invariant and
language checks pass.  Failed candidates are quarantined for diagnosis rather
than silently discarded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import os
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .pdf_pipeline import (
    PdfPreflight,
    PdfPreflightError,
    inspect_pdf,
    publish_candidate,
    repair_pdf_text_cmaps,
    validate_candidate,
    write_pdf_report,
)
from .pdf_table import (
    PdfTable,
    PdfTableCell,
    extract_pdf_tables,
    render_table_translations,
    validate_pdf_table_translations,
)
from .pdf_layout import load_numbering_profile, restore_layout_contract
from .translation_gateway import GatewayConfig, TranslationGateway


class BabelDocPdfTranslationService:
    """Create a candidate with BabelDOC and publish it only after validation."""

    def __init__(self, *, executable: str = "pdf2zh_next") -> None:
        # Keep the executable check for backwards-compatible diagnostics.  The
        # production path below uses pdf2zh-next's supported high-level API so
        # CLI flags cannot drift from the installed library.
        if executable.casefold() in {"pdf2zh_next", "pdf2zh_next.exe", "pdf2zh"}:
            project_worker = Path(sys.executable).with_name("pdf2zh_next.exe")
            if project_worker.is_file():
                self.executable = str(project_worker)
                return
            raise RuntimeError(
                "the project Python environment has no pdf2zh_next executable; "
                "run uv sync --locked before translating a PDF"
            )
        self.executable = executable

    def translate_file(
        self,
        source_path: str | Path,
        destination_path: str | Path,
        *,
        source_language: str,
        target_language: str,
        provider: str,
        model: str,
        glossary: str | Path | None = None,
        style_profile: str | Path | None = None,
        report_path: str | Path | None = None,
        allow_cad_pdf: bool = False,
        allow_complex_pdf: bool = False,
        minimum_font_size: float = 6.0,
        gateway_base_url: str | None = None,
    ) -> tuple[Path, PdfPreflight, Path]:
        source, destination = Path(source_path).resolve(), Path(destination_path).resolve()
        if source == destination:
            raise ValueError("source and destination paths must differ")
        if destination.exists():
            raise FileExistsError("destination already exists; choose a new path")

        report_file = Path(report_path or destination.with_suffix(".pdf-translation.json")).resolve()

        preflight = inspect_pdf(source)
        if preflight.classification in {"D", "E", "F"}:
            error = PdfPreflightError(
                f"PDF class {preflight.classification} is not eligible for automatic translation: "
                + "; ".join(preflight.reasons)
            )
            _write_preflight_rejection_report(report_file, preflight, provider, model, error)
            raise error
        if preflight.classification == "C" and not allow_cad_pdf:
            error = PdfPreflightError(
                "PDF class C requires --allow-cad-pdf after confirming that the source DWG is unavailable"
            )
            _write_preflight_rejection_report(report_file, preflight, provider, model, error)
            raise error
        if preflight.visual_review_required and not allow_complex_pdf:
            error = PdfPreflightError(
                "PDF class B requires --allow-complex-pdf after completing the required visual review"
            )
            _write_preflight_rejection_report(report_file, preflight, provider, model, error)
            raise error

        config = GatewayConfig.from_provider(provider=provider, model=model)
        numbering_profile = load_numbering_profile(style_profile, target_language=target_language)
        destination.parent.mkdir(parents=True, exist_ok=True)

        # pdf2zh-next/BabelDOC create config/cache files during import. Keep
        # this mutable state in the output volume and restore the caller's
        # environment after the job, so one translation cannot leak paths into
        # other document types.
        worker_state = destination.parent / ".pdf_worker_state"
        worker_state.mkdir(parents=True, exist_ok=True)
        worker_env = {
            "USERPROFILE": str(worker_state),
            "HOME": str(worker_state),
            "XDG_CONFIG_HOME": str(worker_state / ".config"),
            "HF_HOME": str(worker_state / ".cache" / "huggingface"),
        }

        gateway: TranslationGateway | None = None
        candidate: Path | None = None
        table_plan: tuple[tuple[PdfTable, ...], dict[str, str], dict[str, object]] | None = None
        run_metadata: dict[str, object] = {
            "engine": "babeldoc.format.pdf.high_level.async_translate",
            "gateway": "local" if gateway_base_url is None else "external",
            "source_hash": preflight.source_hash,
        }
        with tempfile.TemporaryDirectory(
            prefix="document-translator-pdf-", dir=destination.parent
        ) as workdir_text:
            workdir = Path(workdir_text)
            gateway_audit_path = workdir / "gateway-events.jsonl"
            try:
                base_url = gateway_base_url
                if base_url is None:
                    gateway = TranslationGateway(
                        config=config, audit_path=gateway_audit_path
                    ).start()
                    base_url = gateway.base_url
                if not base_url.startswith(
                    ("http://127.0.0.1", "http://localhost", "https://")
                ):
                    raise ValueError(
                        "gateway URL must be a local HTTP endpoint or explicit HTTPS endpoint"
                    )

                # Translate table cells before BabelDOC starts its paragraph
                # batches.  This avoids placing a final table request behind
                # a long burst of page-text calls, which is where providers
                # most commonly return a transient 429/502.  The mapping is
                # retained in memory and only rendered after the candidate is
                # available.
                if preflight.table_pages:
                    table_plan = _prepare_table_translations(
                        source=source,
                        table_pages=preflight.table_pages,
                        source_language=source_language,
                        target_language=target_language,
                        model=model,
                        gateway_url=base_url,
                    )
                    run_metadata["table_route"] = table_plan[2]

                with _temporary_environment(worker_env):
                    result, events = _run_high_level_translation(
                        source=source,
                        workdir=workdir,
                        source_language=source_language,
                        target_language=target_language,
                        model=model,
                        gateway_url=base_url,
                        glossary=glossary,
                    )
                run_metadata.update(events)
                run_metadata.update(_gateway_audit_summary(gateway_audit_path))

                source_candidate = _result_candidate(result, workdir)
                if source_candidate is None:
                    raise RuntimeError(
                        "BabelDOC did not produce a monolingual PDF candidate"
                    )
                candidate = workdir / "validated-candidate.pdf"
                shutil.copy2(source_candidate, candidate)
                if table_plan is not None:
                    table_candidate = workdir / "validated-table-candidate.pdf"
                    table_run = _patch_table_candidate(
                        candidate=candidate,
                        destination=table_candidate,
                        tables=table_plan[0],
                        translations=table_plan[1],
                        target_language=target_language,
                        minimum_font_size=minimum_font_size,
                    )
                    run_metadata["table_route"] = {**table_plan[2], **table_run}
                    candidate = table_candidate
                else:
                    run_metadata["table_route"] = {
                        "status": "not_required",
                        "table_count": 0,
                        "cell_count": 0,
                    }
                layout_candidate = workdir / "validated-layout-candidate.pdf"
                layout_run = restore_layout_contract(
                    source,
                    candidate,
                    layout_candidate,
                    target_language=target_language,
                    profile=numbering_profile,
                    minimum_font_size=minimum_font_size,
                )
                run_metadata["layout_contract"] = layout_run
                candidate = layout_candidate
                run_metadata["cmap_repairs"] = repair_pdf_text_cmaps(candidate)
                validation = validate_candidate(
                    source,
                    candidate,
                    preflight,
                    target_language=target_language,
                    layout_profile=numbering_profile,
                    minimum_font_size=minimum_font_size,
                )
                # Write the audit record before the atomic rename.  If either
                # operation fails, the same quarantine path below retains the
                # candidate and prevents a partial publication.
                report = write_pdf_report(
                    report_file,
                    preflight=preflight,
                    validation=validation,
                    provider=provider,
                    model=model,
                    run=run_metadata,
                )
                published = publish_candidate(candidate, destination)
            except Exception as error:
                run_metadata.update(_gateway_audit_summary(gateway_audit_path))
                quarantine_dir, quarantined_candidate = _quarantine_failure(
                    destination=destination,
                    candidate=candidate,
                    workdir=workdir,
                    run_metadata=run_metadata,
                    error=error,
                )
                _write_failure_report(
                    report_file,
                    preflight=preflight,
                    provider=provider,
                    model=model,
                    error=error,
                    run_metadata=run_metadata,
                    quarantine_dir=quarantine_dir,
                    quarantined_candidate=quarantined_candidate,
                )
                raise
            finally:
                if gateway is not None:
                    gateway.close()
            return published, preflight, report


_LATIN_TARGET_LANGUAGES = frozenset({"en", "en-us", "en-gb", "english"})
_TABLE_GATEWAY_TIMEOUT_SECONDS = 300
# Two cells normally form one complete row in the source documents.  Keeping
# the structured response at one row avoids the truncation seen when a long
# responsibility column is combined with several neighbouring rows.
_TABLE_BATCH_CELL_LIMIT = 2
_TABLE_BATCH_RETRIES = 2


def _translate_and_patch_tables(
    *,
    source: Path,
    candidate: Path,
    destination: Path,
    table_pages: tuple[int, ...],
    source_language: str,
    target_language: str,
    model: str,
    gateway_url: str,
    minimum_font_size: float,
) -> dict[str, object]:
    """Translate native vector-table cells in one structured batch and patch the candidate.

    BabelDOC's paragraph layout remains responsible for the surrounding page.
    Its table text is removed from the candidate and replaced cell-by-cell so
    borders, row heights, and page coordinates remain those of the source PDF.
    The provider sees one semantic batch with stable cell IDs; a missing or
    duplicate ID therefore fails closed before any table output is published.
    """

    tables, translations, preparation = _prepare_table_translations(
        source=source,
        table_pages=table_pages,
        source_language=source_language,
        target_language=target_language,
        model=model,
        gateway_url=gateway_url,
    )
    return {
        **preparation,
        **_patch_table_candidate(
            candidate=candidate,
            destination=destination,
            tables=tables,
            translations=translations,
            target_language=target_language,
            minimum_font_size=minimum_font_size,
        ),
    }


def _prepare_table_translations(
    *,
    source: Path,
    table_pages: tuple[int, ...],
    source_language: str,
    target_language: str,
    model: str,
    gateway_url: str,
) -> tuple[tuple[PdfTable, ...], dict[str, str], dict[str, object]]:
    """Extract tables and obtain one complete stable-ID translation mapping."""

    tables = extract_pdf_tables(source, page_numbers=table_pages)
    if not tables:
        raise RuntimeError(
            "preflight detected table pages but PyMuPDF found no strict vector table"
        )
    cells = tuple(cell for table in tables for cell in table.cells)
    translatable = tuple(cell for cell in cells if not cell.is_empty)
    translations: dict[str, str] = {cell.id: "" for cell in cells if cell.is_empty}
    gateway_batches = 0
    gateway_attempts = 0
    if translatable:
        for batch in _table_translation_batches(translatable, _TABLE_BATCH_CELL_LIMIT):
            request_items = [{"id": cell.id, "input": cell.text} for cell in batch]
            for attempt in range(_TABLE_BATCH_RETRIES + 1):
                gateway_attempts += 1
                try:
                    response = _post_table_translation_batch(
                        gateway_url=gateway_url,
                        model=model,
                        source_language=source_language,
                        target_language=target_language,
                        items=request_items,
                    )
                    translations.update(_normalise_table_response(response))
                    break
                except RuntimeError as error:
                    if (
                        "STRUCTURED_OUTPUT_MAPPING_INVALID" not in str(error)
                        or attempt >= _TABLE_BATCH_RETRIES
                    ):
                        raise
                    time.sleep(min(2.0**attempt, 8.0))
            gateway_batches += 1
    validated = validate_pdf_table_translations(tables, translations)
    return tables, validated, {
        "status": "translated",
        "table_count": len(tables),
        "cell_count": len(cells),
        "translated_cell_count": len(translatable),
        "gateway_batch_size": len(translatable),
        "gateway_batches": gateway_batches,
        "gateway_attempts": gateway_attempts,
    }


def _table_translation_batches(
    cells: tuple[PdfTableCell, ...],
    maximum_cells: int,
) -> tuple[tuple[PdfTableCell, ...], ...]:
    """Group cells by complete table rows for bounded structured responses."""

    if maximum_cells <= 0:
        raise ValueError("maximum_cells must be positive")
    rows: list[list[PdfTableCell]] = []
    row_keys: list[tuple[int, int, int]] = []
    for cell in cells:
        key = (cell.page_number, cell.table_number, cell.row)
        if not row_keys or row_keys[-1] != key:
            row_keys.append(key)
            rows.append([])
        rows[-1].append(cell)

    batches: list[tuple[PdfTableCell, ...]] = []
    current: list[PdfTableCell] = []
    for row in rows:
        if len(row) > maximum_cells:
            if current:
                batches.append(tuple(current))
                current = []
            for offset in range(0, len(row), maximum_cells):
                batches.append(tuple(row[offset : offset + maximum_cells]))
            continue
        if current and len(current) + len(row) > maximum_cells:
            batches.append(tuple(current))
            current = []
        current.extend(row)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _patch_table_candidate(
    *,
    candidate: Path,
    destination: Path,
    tables: tuple[PdfTable, ...],
    translations: dict[str, str],
    target_language: str,
    minimum_font_size: float,
) -> dict[str, object]:
    """Patch an already-created BabelDOC candidate with the table mapping."""

    fontfile = _table_fontfile(target_language)

    def cell_align(cell: PdfTableCell) -> int:
        # Preserve the common source-table convention: header and the narrow
        # role/name column are centred, while long responsibility prose stays
        # left aligned.  The table fitter below reduces wrapping by selecting
        # the readable size that fits the fewest lines, rather than stretching
        # inter-word spacing.
        return 1 if cell.row == 1 or cell.column == 1 else 0

    render_report = render_table_translations(
        candidate,
        destination,
        translations,
        tables=tables,
        fontfile=fontfile,
        minimum_font_size=minimum_font_size,
        initial_font_size=10.0,
        font_step=0.5,
        padding=2.0,
        align=cell_align,
        page_numbers=tuple(sorted({table.page_number for table in tables})),
    )
    return {
        "status": "patched",
        "table_count": render_report.table_count,
        "cell_count": render_report.cell_count,
        "rendered_cell_count": render_report.rendered_cell_count,
        "fontfile": str(fontfile),
        "font_sizes": render_report.font_size_map,
        "drawing_counts": [list(value) for value in render_report.drawing_counts],
    }


def _post_table_translation_batch(
    *,
    gateway_url: str,
    model: str,
    source_language: str,
    target_language: str,
    items: list[dict[str, str]],
) -> object:
    """Send one table-cell batch through the already selected local gateway."""

    system_prompt = (
        "You are translating a PDF table from "
        f"{source_language} to {target_language}. Return ONLY a json array. "
        "For every input item return exactly one object with the same id and "
        "an output field containing the complete translation. Do not omit, "
        "merge, reorder, or invent IDs. Preserve numbers, units, numbering, "
        "punctuation, line breaks where useful, and code-like identifiers. "
        "For document references, translate natural-language issuer text and "
        "reference markers for the target language; preserve only the year, "
        "serial number, and their order. Do not copy source-script text into a "
        "Latin-target result. Keep tags, boundary whitespace, and the complete "
        "translation intact. Keep the translation concise enough to fit its "
        "original cell."
    )
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "Use json output.\n## Here is the input:\n"
                + json.dumps(items, ensure_ascii=False, indent=2),
            },
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
    }
    endpoint = gateway_url.rstrip("/") + "/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=_TABLE_GATEWAY_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except HTTPError as exc:
        # The local gateway already strips provider bodies.  Do the same here
        # so an explicit HTTPS gateway cannot leak document text in an error.
        upstream_code = None
        request_id = None
        gateway_error_type = None
        try:
            diagnostic = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
            if isinstance(diagnostic, dict):
                candidate = diagnostic.get("error_type")
                if isinstance(candidate, str) and len(candidate) <= 128:
                    gateway_error_type = candidate
                candidate = diagnostic.get("upstream_code")
                if isinstance(candidate, str) and len(candidate) <= 128:
                    upstream_code = candidate
                candidate = diagnostic.get("request_id")
                if isinstance(candidate, str) and len(candidate) <= 128:
                    request_id = candidate
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            pass
        suffix = f" ({upstream_code or gateway_error_type})" if (upstream_code or gateway_error_type) else ""
        if request_id:
            suffix += f" request_id={request_id}"
        raise RuntimeError(
            f"table translation gateway returned HTTP {exc.code}{suffix}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("table translation gateway network request failed") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("table translation gateway returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("table translation gateway response is not an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("table translation gateway response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RuntimeError("table translation gateway response has no text content")
    content = message["content"].strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("table translation response was not a JSON array") from exc


def _normalise_table_response(value: object) -> dict[str, str]:
    """Accept only explicit id/output (or id/translation/id/text) items."""

    if not isinstance(value, list):
        raise RuntimeError("table translation response must be a JSON array")
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("table translation response contains a non-object item")
        item_id = item.get("id")
        translated = item.get("output", item.get("translation", item.get("text")))
        if not isinstance(item_id, str) or not item_id:
            raise RuntimeError("table translation response contains an invalid cell ID")
        if item_id in result:
            raise RuntimeError(f"table translation response contains duplicate cell ID: {item_id}")
        if not isinstance(translated, str):
            raise RuntimeError(f"table translation response has no output for cell: {item_id}")
        result[item_id] = (
            translated.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    return result


def _table_fontfile(target_language: str) -> Path:
    """Choose a known installed font for the table overlay, failing closed."""

    if target_language.strip().casefold() in _LATIN_TARGET_LANGUAGES:
        # Long English table cells use the policy's condensed face first so
        # width is recovered before the renderer has to reduce point size.
        candidates = (
            Path(r"C:\Windows\Fonts\ARIALN.TTF"),
            Path(r"C:\Windows\Fonts\LiberationSansNarrow-Regular.ttf"),
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path(r"C:\Windows\Fonts\ARIALUNI.ttf"),
        )
    else:
        candidates = (
            Path(r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf"),
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path(r"C:\Windows\Fonts\msyh.ttc"),
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "no installed font is available for the PDF table overlay; "
        + ", ".join(str(path) for path in candidates)
    )


@contextlib.contextmanager
def _temporary_environment(updates: dict[str, str]):
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _terminate_worker_tree(process: subprocess.Popen[str]) -> None:
    """Stop the project worker and any children it created on Windows.

    ``Popen.terminate`` only signals the direct worker.  BabelDOC may create
    multiprocessing children, and those children otherwise survive an
    interrupted CLI run.  ``taskkill /T`` is scoped to this exact PID and
    therefore does not affect unrelated Python/Office processes.
    """

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_high_level_translation(
    *,
    source: Path,
    workdir: Path,
    source_language: str,
    target_language: str,
    model: str,
    gateway_url: str,
    glossary: str | Path | None,
) -> tuple[object, dict[str, object]]:
    """Run BabelDOC in a dedicated project worker subprocess."""

    payload = {
        "source": str(source),
        "workdir": str(workdir),
        "source_language": source_language,
        "target_language": target_language,
        "model": model,
        "gateway_url": gateway_url,
        "glossary": str(Path(glossary).resolve()) if glossary is not None else None,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "document_translator.pdf_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload, ensure_ascii=False))
    process.stdin.close()
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_worker_output() -> None:
        assert process.stdout is not None
        for output_line in process.stdout:
            events.put(("line", output_line))
        events.put(("eof", None))

    threading.Thread(target=read_worker_output, name="pdf-worker-output", daemon=True).start()
    finish_result: object | None = None
    progress_events = 0
    last_stage: str | None = None
    token_usage: object | None = None
    deadline = time.monotonic() + 30 * 60
    try:
        while True:
            remaining = max(0.1, min(1.0, deadline - time.monotonic()))
            if remaining <= 0:
                raise TimeoutError("PDF worker timed out after 1800 seconds")
            try:
                kind, line = events.get(timeout=remaining)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError("PDF worker timed out after 1800 seconds")
                continue
            if kind == "eof":
                break
            assert line is not None
            if not line.strip():
                continue
            event = json.loads(line)
            progress_events += 1
            if isinstance(event.get("stage"), str):
                last_stage = event["stage"]
            if event.get("type") == "error":
                raise RuntimeError(str(event.get("error", "BabelDOC translation failed")))
            if event.get("type") == "finish":
                finish_result = event.get("translate_result")
                token_usage = event.get("token_usage")
                break
        if process.poll() not in (None, 0) and finish_result is None:
            raise RuntimeError(f"PDF worker exited with code {process.returncode}")
        if finish_result is None:
            raise RuntimeError("BabelDOC ended without a finish event")
        metadata: dict[str, object] = {
            "progress_events": progress_events,
            "last_stage": last_stage,
            "worker_mode": "project_subprocess_babeldoc",
        }
        if token_usage is not None:
            metadata["token_usage"] = token_usage
        return finish_result, metadata
    finally:
        _terminate_worker_tree(process)


def _run_coroutine(factory: Callable[[], Any]) -> Any:
    """Run a coroutine from both normal CLI code and an active event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    # A library caller may invoke the synchronous service from an async UI
    # callback. Run the private loop on a short-lived thread in that case.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(factory())).result()


def _result_candidate(result: object, workdir: Path) -> Path | None:
    candidates: list[Path] = []
    for attr in ("mono_pdf_path", "no_watermark_mono_pdf_path"):
        value = result.get(attr) if isinstance(result, dict) else getattr(result, attr, None)
        if value:
            path = Path(value)
            if path.is_file() and path.suffix.casefold() == ".pdf":
                candidates.append(path)
    if not candidates:
        candidates.extend(sorted(workdir.glob("*.mono.pdf")))
        candidates.extend(sorted(workdir.glob("*-mono.pdf")))
    unique = list(dict.fromkeys(path.resolve() for path in candidates))
    return unique[0] if len(unique) == 1 else None


def _quarantine_failure(
    *,
    destination: Path,
    candidate: Path | None,
    workdir: Path,
    run_metadata: dict[str, object],
    error: Exception,
) -> tuple[Path, Path | None]:
    quarantine_dir = (
        destination.parent
        / ".pdf_quarantine"
        / f"{destination.stem}-{uuid.uuid4().hex[:12]}"
    )
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    quarantined_candidate: Path | None = None
    if candidate is not None and candidate.is_file():
        quarantined_candidate = quarantine_dir / "candidate.pdf"
        shutil.copy2(candidate, quarantined_candidate)
    audit_path = workdir / "gateway-events.jsonl"
    if audit_path.is_file():
        shutil.copy2(audit_path, quarantine_dir / audit_path.name)
    (quarantine_dir / "run.json").write_text(
        json.dumps(
            {
                "error": str(error),
                "error_type": type(error).__name__,
                "run": run_metadata,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return quarantine_dir, quarantined_candidate


def _gateway_audit_summary(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"gateway_requests": 0, "gateway_batch_sizes": []}
    records: list[dict[str, object]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"gateway_requests": 0, "gateway_batch_sizes": [], "gateway_audit_read_error": True}
    requests = [record for record in records if record.get("event") != "upstream_result"]
    sizes = [record.get("batch_count") for record in requests if isinstance(record.get("batch_count"), int)]
    return {"gateway_requests": len(requests), "gateway_batch_sizes": sizes}


def _write_preflight_rejection_report(
    path: Path,
    preflight: PdfPreflight,
    provider: str,
    model: str,
    error: Exception,
) -> None:
    """Persist a safe report when the source is rejected before cloud work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "REJECTED_PREFLIGHT",
                "preflight": {
                    "source_hash": preflight.source_hash,
                    "page_count": preflight.page_count,
                    "page_sizes": preflight.page_sizes,
                    "classification": preflight.classification,
                    "reasons": preflight.reasons,
                    "table_pages": preflight.table_pages,
                    "visual_review_required": preflight.visual_review_required,
                },
                "provider": provider,
                "model": model,
                "error": str(error),
                "error_type": type(error).__name__,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def _write_failure_report(
    path: Path,
    *,
    preflight: PdfPreflight,
    provider: str,
    model: str,
    error: Exception,
    run_metadata: dict[str, object],
    quarantine_dir: Path,
    quarantined_candidate: Path | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "FAILED",
        "preflight": {
            "source_hash": preflight.source_hash,
            "page_count": preflight.page_count,
            "page_sizes": preflight.page_sizes,
            "classification": preflight.classification,
            "reasons": preflight.reasons,
            "table_pages": preflight.table_pages,
            "visual_review_required": preflight.visual_review_required,
        },
        "provider": provider,
        "model": model,
        "error": str(error),
        "error_type": type(error).__name__,
        "run": run_metadata,
        "quarantine_dir": str(quarantine_dir),
        "quarantined_candidate": str(quarantined_candidate) if quarantined_candidate else None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
