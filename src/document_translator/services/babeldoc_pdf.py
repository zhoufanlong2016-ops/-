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
import hashlib
import json
import math
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
    normalize_unicode_dashes,
    remove_control_characters,
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
from document_translator.providers.translation_prompt import general_translation_instruction
from document_translator.translation_rules import source_name_constraints, validate_name_retention, validate_translation_residue, auto_correct_translation


def _worker_python() -> str:
    """The interpreter that has BabelDOC installed.

    BabelDOC pins its own PyMuPDF/pdf2zh-next versions, so it lives in a
    separate environment (``.venv-babeldoc`` next to the project, or the
    path in DOCUMENT_TRANSLATOR_BABELDOC_PYTHON) rather than beside MinerU.
    """
    configured = os.environ.get("DOCUMENT_TRANSLATOR_BABELDOC_PYTHON")
    if configured:
        return configured
    if not getattr(sys, "frozen", False):
        candidate = Path(__file__).resolve().parents[3] / ".venv-babeldoc" / "Scripts" / "python.exe"
        if candidate.is_file():
            return str(candidate)
    return sys.executable


class BabelDocPdfTranslationService:
    """Create a candidate with BabelDOC and publish it only after validation."""

    def __init__(self, *, executable: str = "pdf2zh_next") -> None:
        # Keep the executable check for backwards-compatible diagnostics.  The
        # production path below uses pdf2zh-next's supported high-level API so
        # CLI flags cannot drift from the installed library.
        if executable.casefold() in {"pdf2zh_next", "pdf2zh_next.exe", "pdf2zh"}:
            # A PyInstaller build runs the project-owned ``pdf_worker`` from
            # the frozen bundle and imports pdf2zh-next as a bundled module;
            # there is no separate venv Scripts directory to probe.
            if getattr(sys, "frozen", False):
                self.executable = executable
                return
            project_worker = Path(_worker_python()).with_name("pdf2zh_next.exe")
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
        job_started_at = time.monotonic()
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

        # Keep BabelDOC's mutable job state beside the output, but keep its
        # large immutable assets in the user's stable cache.  Previously HOME
        # pointed at this per-job directory, so every PDF redownloaded all
        # fonts/CMaps/model assets during the translation.
        worker_state = destination.parent / ".pdf_worker_state"
        worker_state.mkdir(parents=True, exist_ok=True)
        asset_home = Path.home()
        missing_assets = _missing_babeldoc_assets(asset_home)
        if missing_assets:
            preview = ", ".join(missing_assets[:5])
            suffix = " ..." if len(missing_assets) > 5 else ""
            raise RuntimeError(
                "BabelDOC resources are not prepared; run the separate PDF resource preparation "
                f"step before translating. Missing: {preview}{suffix}"
            )
        worker_env = {
            "USERPROFILE": str(asset_home),
            "HOME": str(asset_home),
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
            "timing_seconds": {},
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
                    table_started_at = time.monotonic()
                    try:
                        table_plan = _prepare_table_translations(
                            source=source,
                            table_pages=preflight.table_pages,
                            source_language=source_language,
                            target_language=target_language,
                            model=model,
                            gateway_url=base_url,
                        )
                        run_metadata["table_route"] = table_plan[2]
                    except RuntimeError as exc:
                        # A cell that still fails validation after correction
                        # leaves BabelDOC's own table text in place (reported),
                        # like the other engines, instead of failing the job.
                        table_plan = None
                        run_metadata["table_route"] = {"status": "failed", "reason": str(exc)}
                    run_metadata["timing_seconds"]["table_translation"] = round(time.monotonic() - table_started_at, 3)

                worker_started_at = time.monotonic()
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
                run_metadata["timing_seconds"]["babeldoc_translation"] = round(time.monotonic() - worker_started_at, 3)
                run_metadata.update(_gateway_audit_summary(gateway_audit_path))

                source_candidate = _result_candidate(result, workdir)
                if source_candidate is None:
                    raise RuntimeError(
                        "BabelDOC did not produce a monolingual PDF candidate"
                    )
                candidate = workdir / "validated-candidate.pdf"
                shutil.copy2(source_candidate, candidate)
                if table_plan is not None:
                    table_render_started_at = time.monotonic()
                    table_candidate = workdir / "validated-table-candidate.pdf"
                    try:
                        table_run = _patch_table_candidate(
                            candidate=candidate,
                            destination=table_candidate,
                            tables=table_plan[0],
                            translations=table_plan[1],
                            target_language=target_language,
                            minimum_font_size=minimum_font_size,
                        )
                    except (RuntimeError, ValueError) as exc:
                        # Same rule as a failed table translation: keep
                        # BabelDOC's own table text and report it.
                        run_metadata["table_route"] = {**table_plan[2], "status": "patch_failed", "reason": str(exc)}
                    else:
                        run_metadata["table_route"] = {**table_plan[2], **table_run}
                        candidate = table_candidate
                    run_metadata["timing_seconds"]["table_render"] = round(time.monotonic() - table_render_started_at, 3)
                elif "table_route" not in run_metadata:
                    run_metadata["table_route"] = {
                        "status": "not_required",
                        "table_count": 0,
                        "cell_count": 0,
                    }
                rotated_candidate = workdir / "validated-rotated-candidate.pdf"
                rotated_started_at = time.monotonic()
                rotated_run = _translate_and_patch_rotated_text(
                    source=source,
                    candidate=candidate,
                    destination=rotated_candidate,
                    source_language=source_language,
                    target_language=target_language,
                    model=model,
                    gateway_url=base_url,
                    glossary=glossary,
                )
                run_metadata["rotated_text_route"] = rotated_run
                run_metadata["timing_seconds"]["rotated_text"] = round(time.monotonic() - rotated_started_at, 3)
                candidate = rotated_candidate
                layout_candidate = workdir / "validated-layout-candidate.pdf"
                layout_started_at = time.monotonic()
                layout_run = restore_layout_contract(
                    source,
                    candidate,
                    layout_candidate,
                    target_language=target_language,
                    profile=numbering_profile,
                    minimum_font_size=minimum_font_size,
                )
                run_metadata["layout_contract"] = layout_run
                run_metadata["timing_seconds"]["layout_restore"] = round(time.monotonic() - layout_started_at, 3)
                candidate = layout_candidate
                run_metadata["deterministic_label_repairs"] = _repair_deterministic_pdf_labels(
                    candidate, target_language=target_language
                )
                validation_started_at = time.monotonic()
                run_metadata["cmap_repairs"] = repair_pdf_text_cmaps(candidate)
                validation = validate_candidate(
                    source,
                    candidate,
                    preflight,
                    target_language=target_language,
                    layout_profile=numbering_profile,
                    minimum_font_size=minimum_font_size,
                )
                run_metadata["timing_seconds"]["validation_and_cmap"] = round(time.monotonic() - validation_started_at, 3)
                run_metadata["timing_seconds"]["total"] = round(time.monotonic() - job_started_at, 3)
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
                run_metadata["timing_seconds"]["total_until_failure"] = round(time.monotonic() - job_started_at, 3)
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
# Send a complete bounded table context to the model.  The former two-cell
# limit turned a 20-cell table into ten serial API requests; stable IDs make a
# 24-cell response equally verifiable while dramatically reducing latency.
# Keep ordinary tables in a small number of requests, but cap the estimated
# prompt body so one unusually long responsibility cell cannot occupy a
# 10k-character request and wait for the provider's 120-second timeout.
_TABLE_BATCH_CELL_LIMIT = 24
_TABLE_BATCH_CHAR_LIMIT = 6000
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
    batch_cell_limit, batch_char_limit, cell_text_limit, output_token_limit = _table_model_limits(model)
    if translatable:
        for batch in _table_translation_batches(
            translatable,
            batch_cell_limit,
            maximum_chars=batch_char_limit,
        ):
            request_groups, part_ids = _expand_long_table_cells(
                batch,
                batch_char_limit,
                maximum_text_chars=cell_text_limit,
            )
            response_map: dict[str, str] = {}
            for request_items in request_groups:
                for attempt in range(_TABLE_BATCH_RETRIES + 1):
                    gateway_attempts += 1
                    try:
                        response = _post_table_translation_batch(
                            gateway_url=gateway_url,
                            model=model,
                            source_language=source_language,
                            target_language=target_language,
                            items=request_items,
                            max_output_tokens=output_token_limit,
                        )
                        mapped = _normalise_table_response(response)
                        if set(mapped) != {item["id"] for item in request_items}:
                            raise RuntimeError("table translation response IDs are incomplete or unknown")
                        response_map.update(mapped)
                        break
                    except RuntimeError as error:
                        if (
                            "STRUCTURED_OUTPUT_MAPPING_INVALID" not in str(error)
                            or attempt >= _TABLE_BATCH_RETRIES
                        ):
                            raise
                        time.sleep(min(2.0**attempt, 8.0))
                gateway_batches += 1
            for cell in batch:
                ids = part_ids[cell.id]
                missing = [item_id for item_id in ids if item_id not in response_map]
                if missing:
                    raise RuntimeError(f"table translation response missing cell parts: {missing[0]}")
                translations[cell.id] = "".join(response_map[item_id] for item_id in ids)
    validated = validate_pdf_table_translations(tables, translations, source_language=source_language, target_language=target_language)
    return tables, validated, {
        "status": "translated",
        "table_count": len(tables),
        "cell_count": len(cells),
        "translated_cell_count": len(translatable),
        "gateway_batch_size": len(translatable),
        "gateway_batches": gateway_batches,
        "gateway_attempts": gateway_attempts,
    }


def _table_model_limits(model: str) -> tuple[int, int, int, int]:
    """Return conservative table limits for each provider/model family.

    Qwen-plus has been observed timing out on a single roughly 8k-character
    cell.  Keep its prompts and parts smaller; larger-context models can use
    more cells, while the default remains conservative for unknown models.
    """
    name = (model or "").casefold()
    if "qwen-plus" in name:
        return 16, 4500, 3000, 4096
    if "qwen-max" in name:
        return 24, 6000, 4500, 8192
    if name.startswith("qwen"):
        return 20, 5000, 3500, 8192
    if name.startswith("gpt-5.6-sol"):
        return 24, 10000, 6000, 8192
    if name.startswith("gpt-5.6-terra"):
        return 24, 8000, 5000, 8192
    if name.startswith("gpt-5.6-luna"):
        return 20, 6000, 4000, 4096
    if name.startswith("gpt-5") or name.startswith("o"):
        return 20, 6000, 4000, 4096
    return _TABLE_BATCH_CELL_LIMIT, _TABLE_BATCH_CHAR_LIMIT, 4500, 4096


def _expand_long_table_cells(
    cells: tuple[PdfTableCell, ...],
    maximum_chars: int,
    *,
    maximum_text_chars: int | None = None,
) -> tuple[list[list[dict[str, str]]], dict[str, list[str]]]:
    """Split oversized cell text into stable ordered sub-items.

    A single long cell cannot be made safe by cell-count batching.  Split at
    whitespace/newline boundaries where possible, retain every source
    character, and reassemble the translated parts locally after validation.
    """
    request_items: list[dict[str, str]] = []
    part_ids: dict[str, list[str]] = {}
    # Reserve room for the system prompt, JSON indentation and the stable-ID
    # envelope; the gateway audit's prompt length includes all of these.
    per_item_overhead = 1500
    max_text = max(512, maximum_text_chars or maximum_chars - per_item_overhead)
    for cell in cells:
        text = cell.text
        parts: list[str] = []
        start = 0
        while len(text) - start > max_text:
            end = start + max_text
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary <= start + max_text // 2:
                boundary = end
            parts.append(text[start:boundary])
            start = boundary
        parts.append(text[start:])
        ids = [cell.id if len(parts) == 1 else f"{cell.id}::part-{index + 1}" for index in range(len(parts))]
        part_ids[cell.id] = ids
        request_items.extend(
            {"id": item_id, "input": part}
            for item_id, part in zip(ids, parts, strict=True)
        )
    groups: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    estimated = 0
    for item in request_items:
        cost = len(item["input"]) + 96
        if current and estimated + cost > maximum_chars:
            groups.append(current)
            current = []
            estimated = 0
        current.append(item)
        estimated += cost
    if current:
        groups.append(current)
    return groups, part_ids


def _table_translation_batches(
    cells: tuple[PdfTableCell, ...],
    maximum_cells: int,
    *,
    maximum_chars: int | None = None,
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
    if maximum_chars is None or maximum_chars <= 0:
        return tuple(batches)

    bounded: list[tuple[PdfTableCell, ...]] = []
    for batch in batches:
        current: list[PdfTableCell] = []
        estimated = 0
        for cell in batch:
            # JSON quoting and the id/input envelope add overhead beyond the
            # source text.  Keep a conservative fixed allowance per cell.
            cost = len(cell.text) + 96
            if current and estimated + cost > maximum_chars:
                bounded.append(tuple(current))
                current = []
                estimated = 0
            current.append(cell)
            estimated += cost
        if current:
            bounded.append(tuple(current))
    return tuple(bounded)


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
    max_output_tokens: int = 4096,
) -> object:
    """Send one table-cell batch through the already selected local gateway."""

    items = [{**item, "required_names": source_name_constraints(item["input"], source_language, target_language)} for item in items]

    system_prompt = (
        general_translation_instruction(source_language, target_language)
        + "\n\nPDF table batch contract: return ONLY a json array. "
        "For every input item return exactly one object with the same id and "
        "an output field containing the complete translation. Do not omit, "
        "merge, reorder, or invent IDs. Preserve numbers, units, numbering, "
        "punctuation, line breaks where useful, and code-like identifiers. "
        "Do not copy source-script text into a Latin-target result. Keep tags, "
        "boundary whitespace, and the complete translation intact. Keep the "
        "translation concise enough to fit its "
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
        "max_tokens": max_output_tokens,
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
        parsed = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("table translation response was not a JSON array") from exc
    mapped = _normalise_table_response(parsed)
    if set(mapped) != {item["id"] for item in items}:
        raise RuntimeError("table translation response IDs are incomplete or unknown")
    from document_translator.core.validation import validate_placeholders
    from document_translator.translation_rules import rule_protected_tokens
    for item in items:
        errors = validate_name_retention(item["input"], mapped[item["id"]], source_language, target_language)
        errors.extend(validate_placeholders(item["input"], mapped[item["id"]], rule_protected_tokens(item["input"])))
        if errors:
            raise RuntimeError("table translation validation failed: " + "; ".join(errors))
    return parsed


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
            normalize_unicode_dashes(remove_control_characters(translated))
            .replace("\\r\\n", "\n")
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
            # The Windows Noto SC OTF is present on this machine but its
            # cmap is decoded incorrectly by PyMuPDF when used as a newly
            # inserted font.  BabelDOC's static Source Han TTF is the same
            # family with a reliable Unicode cmap.
            Path.home() / ".cache" / "babeldoc" / "fonts" / "SourceHanSansCN-Regular.ttf",
            Path.home() / ".cache" / "babeldoc" / "fonts" / "SourceHanSansCN-Bold.ttf",
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


def _missing_babeldoc_assets(home: Path) -> list[str]:
    """Return missing/corrupt BabelDOC assets without starting a download."""
    try:
        from babeldoc.assets.assets import generate_all_assets_file_list
    except ImportError:
        # BabelDOC lives in its own environment; ask that interpreter.
        if _worker_python() == sys.executable:
            return ["babeldoc asset metadata"]
        package_root = str(Path(__file__).resolve().parents[2])
        script = (
            "import json,sys;from pathlib import Path;"
            "from document_translator.services.babeldoc_pdf import _missing_babeldoc_assets as m;"
            "print(json.dumps(m(Path(sys.argv[1]))))"
        )
        result = subprocess.run(
            [_worker_python(), "-c", script, str(home)],
            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": package_root},
        )
        try:
            return list(json.loads(result.stdout.strip().splitlines()[-1]))
        except (IndexError, ValueError):
            return ["babeldoc asset metadata"]

    folders = {
        "fonts": home / ".cache" / "babeldoc" / "fonts",
        "models": home / ".cache" / "babeldoc" / "models",
        "tiktoken": home / ".cache" / "babeldoc" / "tiktoken",
        "cmap": home / ".cache" / "babeldoc" / "cmap",
    }
    missing: list[str] = []
    for category, entries in generate_all_assets_file_list().items():
        folder = folders[category]
        for entry in entries:
            path = folder / entry["name"]
            if not path.is_file():
                missing.append(f"{category}/{entry['name']}")
                continue
            digest = hashlib.sha3_256(path.read_bytes()).hexdigest()
            if digest != entry["sha3_256"]:
                missing.append(f"{category}/{entry['name']} (checksum)")
    return missing


def _rotated_text_pages(source: Path) -> tuple[int, ...]:
    """Return zero-based pages whose source text uses an arbitrary angle."""
    try:
        import fitz
        document = fitz.open(source)
    except Exception:
        return ()
    pages: list[int] = []
    try:
        for index, page in enumerate(document):
            rotated = False
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    direction = line.get("dir")
                    if not direction or len(direction) < 2:
                        continue
                    dx, dy = float(direction[0]), float(direction[1])
                    # Horizontal and vertical text are supported by BabelDOC;
                    # any other angle would be flattened by its IL renderer.
                    if abs(dx) > 0.05 and abs(dy) > 0.05:
                        rotated = True
                        break
                if rotated:
                    break
            if rotated:
                pages.append(index)
    finally:
        document.close()
    return tuple(pages)


def _rotated_text_items(source: Path) -> list[dict[str, object]]:
    """Extract arbitrary-angle text lines with enough geometry to redraw them."""
    import fitz

    document = fitz.open(source)
    items: list[dict[str, object]] = []
    try:
        rotated_pages: set[int] = set()
        for page_number, page in enumerate(document, 1):
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    direction = line.get("dir")
                    if direction and len(direction) >= 2:
                        dx, dy = float(direction[0]), float(direction[1])
                        if abs(dx) > 0.05 and abs(dy) > 0.05:
                            rotated_pages.add(page_number)
                            break
                if page_number in rotated_pages:
                    break
        for page_number, page in enumerate(document, 1):
            if page_number not in rotated_pages:
                continue
            for block_number, block in enumerate(page.get_text("dict").get("blocks", [])):
                if block.get("type") != 0:
                    continue
                for line_number, line in enumerate(block.get("lines", [])):
                    direction = line.get("dir")
                    text = "".join(str(span.get("text", "")) for span in line.get("spans", [])).strip()
                    if not text or not direction or len(direction) < 2:
                        continue
                    dx, dy = float(direction[0]), float(direction[1])
                    spans = [span for span in line.get("spans", []) if str(span.get("text", "")).strip()]
                    if not spans:
                        continue
                    origin = tuple(float(value) for value in spans[0].get("origin", line.get("bbox", (0, 0))[:2]))
                    fontsize = max(float(span.get("size", 9.0) or 9.0) for span in spans)
                    color_value = int(spans[0].get("color", 0) or 0)
                    color = ((color_value >> 16 & 255) / 255.0, (color_value >> 8 & 255) / 255.0, (color_value & 255) / 255.0)
                    quads = []
                    for span in spans:
                        try:
                            quad = fitz.recover_quad((dx, dy), span)
                            quads.append(tuple((float(point.x), float(point.y)) for point in quad))
                        except Exception:
                            quads = []
                            break
                    if not quads:
                        continue
                    item_id = hashlib.sha256(
                        f"{page_number}:{block_number}:{line_number}:{text}".encode("utf-8")
                    ).hexdigest()
                    items.append({
                        "id": item_id,
                        "input": text,
                        "page": page_number,
                        "origin": origin,
                        "angle": math.degrees(math.atan2(-dy, dx)),
                        "fontsize": fontsize,
                        "color": color,
                        "quads": quads,
                    })
    finally:
        document.close()
    return items


def _post_rotated_translation_batch(
    *, gateway_url: str, model: str, source_language: str,
    target_language: str, items: list[dict[str, object]],
    glossary: object | None = None,
    correction: str = "",
) -> dict[str, str]:
    """Translate rotated labels through the same structured gateway contract."""
    system = (
        general_translation_instruction(source_language, target_language)
        + "\n\nReturn only a JSON array. Each item must contain the same id and an "
        "output field. Do not omit, merge, reorder, or invent items. Preserve "
        "all numbers, units, identifiers, drawing references, and supplied terms."
    )
    if correction:
        system += "\nAUTOMATIC CORRECTION: " + correction
    from document_translator.translation_rules import rule_protected_tokens, protect_for_translation, restore_after_translation
    protected = {str(item["id"]): protect_for_translation(str(item["input"]), rule_protected_tokens(str(item["input"]))) for item in items}
    if glossary is not None:
        from document_translator.services.glossary import Glossary
        glossary_obj = glossary if isinstance(glossary, Glossary) else Glossary.load(str(glossary))
        terms = []
        for item in items:
            terms.extend((entry.source, entry.target) for entry in glossary_obj.entries_for(str(item["input"])))
        terms = list(dict.fromkeys(terms))
        if terms:
            system += "\n\nRequired terminology (mandatory):\n" + "\n".join(
                f"- {source} -> {target}" for source, target in terms
            )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "## Here is the input:\n" + json.dumps(
                [{"id": item["id"], "input": protected[str(item["id"])].text, "required_names": source_name_constraints(str(item["input"]), source_language, target_language)} for item in items],
                ensure_ascii=False,
            )},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
    }
    request = Request(
        gateway_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=_TABLE_GATEWAY_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("rotated text translation gateway request failed") from exc
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(str(content).strip().removeprefix("```json").removesuffix("```").strip())
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("rotated text translation response was not valid JSON") from exc
    rows = parsed.get("items") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise RuntimeError("rotated text translation response is not an array")
    expected = {str(item["id"]): str(item["input"]) for item in items}
    item_by_id = {str(item["id"]): item for item in items}
    mapped: dict[str, str] = {}
    item_errors: dict[str, list[str]] = {}
    from document_translator.core.validation import validate_placeholders
    from document_translator.translation_rules import rule_protected_tokens
    glossary_obj = None
    if glossary is not None:
        from document_translator.services.glossary import Glossary
        glossary_obj = glossary if isinstance(glossary, Glossary) else Glossary.load(str(glossary))
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("output"), str):
            raise RuntimeError("rotated text translation item is invalid")
        item_id = row["id"]
        if item_id in mapped or item_id not in expected:
            raise RuntimeError("rotated text translation IDs are incomplete")
        try:
            output = restore_after_translation(row["output"], protected[item_id])
        except ValueError as exc:
            if correction:
                raise RuntimeError("rotated text correction failed to preserve protected literals") from exc
            repaired = _post_rotated_translation_batch(
                gateway_url=gateway_url, model=model, source_language=source_language,
                target_language=target_language, items=[item_by_id[item_id],], glossary=glossary,
                correction="Return every [[TRP_nnnn]] marker unchanged, exactly once. Do not translate or omit protected markers.",
            )
            mapped.update(repaired)
            continue
        output = auto_correct_translation(expected[item_id], output, source_language, target_language)
        errors = validate_placeholders(expected[item_id], output, rule_protected_tokens(expected[item_id]))
        errors.extend(validate_name_retention(expected[item_id], output, source_language, target_language))
        errors.extend(validate_translation_residue(expected[item_id], output, source_language, target_language))
        if glossary_obj is not None:
            for entry in glossary_obj.entries_for(expected[item_id]):
                if output.casefold().count(entry.target.casefold()) < expected[item_id].count(entry.source):
                    errors.append(f"GLOSSARY_TERM_MISSING: {entry.source!r} -> {entry.target!r}")
        if errors:
            item_errors[item_id] = errors
            continue
        mapped[item_id] = output
    if item_errors:
        details = "; ".join(
            f"{item_id}: {error}" for item_id, errors in item_errors.items() for error in errors
        )
        if correction:
            raise RuntimeError("rotated text translation validation failed: " + details)
        repaired = _post_rotated_translation_batch(
            gateway_url=gateway_url, model=model, source_language=source_language,
            target_language=target_language,
            items=[item_by_id[item_id] for item_id in item_errors], glossary=glossary,
            correction=details,
        )
        mapped.update(repaired)
    if set(mapped) != set(expected):
        raise RuntimeError("rotated text translation IDs are incomplete")
    return mapped


def _translate_and_patch_rotated_text(
    *, source: Path, candidate: Path, destination: Path,
    source_language: str, target_language: str, model: str, gateway_url: str,
    glossary: str | Path | None = None,
) -> dict[str, object]:
    """Translate arbitrary-angle source lines and redraw them on the candidate."""
    items = _rotated_text_items(source)
    if not items:
        shutil.copy2(candidate, destination)
        return {"status": "not_required", "item_count": 0, "batch_count": 0}
    translations: dict[str, str] = {}
    from document_translator.translation_rules import rule_protected_tokens
    translatable = []
    for item in items:
        text = str(item["input"])
        tokens = rule_protected_tokens(text)
        remainder = text
        for token in sorted(tokens, key=len, reverse=True):
            remainder = remainder.replace(token, "")
        if not any(char.isalpha() for char in remainder):
            translations[str(item["id"])] = text
        else:
            translatable.append(item)
    for start in range(0, len(translatable), 24):
        translations.update(_post_rotated_translation_batch(
            gateway_url=gateway_url, model=model, source_language=source_language,
            target_language=target_language, items=translatable[start : start + 24], glossary=glossary,
        ))
    import fitz
    document = fitz.open(candidate)
    source_document = fitz.open(source)
    try:
        pages: dict[int, list[dict[str, object]]] = {}
        for item in items:
            pages.setdefault(int(item["page"]), []).append(item)
        fontfile = _table_fontfile(target_language)
        for page_number, page_items in pages.items():
            # BabelDOC intentionally skips arbitrary-angle pages, but its
            # later cleanup can still drop axis-aligned labels. Replace the
            # whole page with the untouched source page before redrawing all
            # extracted labels, so no map text or drawing geometry is lost.
            document.delete_page(page_number - 1)
            document.insert_pdf(source_document, from_page=page_number - 1, to_page=page_number - 1, start_at=page_number - 1)
            page = document[page_number - 1]
            for item in page_items:
                for points in item["quads"]:
                    # A map label is drawn over map imagery. Transparent
                    # text-only redaction must not paint a white rectangle
                    # over that imagery or over neighbouring labels.
                    page.add_redact_annot(fitz.Quad(points), fill=False)
            # Remove only text operators.  Map lines, fills and images are
            # part of the drawing and must survive the label replacement.
            page.apply_redactions(images=0, graphics=0, text=0)
            for item in page_items:
                origin = fitz.Point(*item["origin"])
                page.insert_text(
                    origin,
                    translations[str(item["id"])],
                    fontname="pdfrotcjk",
                    fontfile=str(fontfile),
                    fontsize=float(item["fontsize"]),
                    # PyMuPDF's ``rotate`` argument accepts only page-like
                    # right angles for insert_text.  ``morph`` is the
                    # supported arbitrary-angle path for CAD/map labels.
                    morph=(origin, fitz.Matrix(float(item["angle"]))),
                    color=item["color"],
                    overlay=True,
                )
        document.save(destination, garbage=1, deflate=True)
    finally:
        source_document.close()
        document.close()
    return {"status": "patched", "item_count": len(items), "translated_item_count": len(translatable), "batch_count": (len(translatable) + 23) // 24}


def _repair_deterministic_pdf_labels(path: Path, *, target_language: str) -> dict[str, object]:
    """Repair deterministic label/date residues left by BabelDOC fallbacks."""
    if not target_language.casefold().startswith("zh"):
        return {"status": "not_required", "repairs": []}
    import fitz
    from document_translator.translation_rules import auto_correct_translation

    document = fitz.open(path)
    repairs: list[dict[str, object]] = []
    fontfile = _table_fontfile(target_language)
    try:
        for page_number, page in enumerate(document, 1):
            pending: list[tuple[fitz.Point, fitz.Rect, str, float, tuple[float, float, float]]] = []
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = str(span.get("text", ""))
                        corrected = auto_correct_translation(text, text, "en", target_language)
                        if corrected == text or not text.strip():
                            continue
                        bbox = fitz.Rect(span["bbox"])
                        color_value = int(span.get("color", 0) or 0)
                        color = (
                            (color_value >> 16 & 255) / 255.0,
                            (color_value >> 8 & 255) / 255.0,
                            (color_value & 255) / 255.0,
                        )
                        origin = fitz.Point(*span.get("origin", bbox[:2]))
                        pending.append((origin, bbox, corrected, float(span.get("size", 9.0) or 9.0), color))
            if not pending:
                continue
            for _origin, bbox, _corrected, _size, _color in pending:
                page.add_redact_annot(bbox, fill=False)
            page.apply_redactions(images=0, graphics=0, text=0)
            for origin, _bbox, corrected, size, color in pending:
                page.insert_text(
                    origin, corrected, fontname="pdfautocjk", fontfile=str(fontfile),
                    fontsize=size, color=color, overlay=True,
                )
                repairs.append({"page": page_number, "to": corrected})
        result = {"status": "repaired" if repairs else "not_required", "repairs": repairs}
        if repairs:
            temporary = path.with_suffix(path.suffix + ".deterministic.tmp")
            document.save(temporary, garbage=1, deflate=True)
            document.close()
            temporary.replace(path)
        return result
    finally:
        try:
            document.close()
        except Exception:
            pass


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
        "skip_translation_pages": list(_rotated_text_pages(source)),
    }
    package_root = str(Path(__file__).resolve().parents[2])
    worker_env = dict(os.environ)
    worker_env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, worker_env.get("PYTHONPATH")]))
    process = subprocess.Popen(
        [_worker_python(), "-m", "document_translator.pdf_worker"],
        env=worker_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        # Keep the worker protocol byte-oriented.  On Windows, using a
        # TextIOWrapper here can normalize/rewrap backslashes before the
        # worker's JSON decoder sees them.
        text=False,
        bufsize=0,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    process.stdin.close()
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_worker_output() -> None:
        assert process.stdout is not None
        for output_line in process.stdout:
            events.put(("line", output_line.decode("utf-8", errors="replace")))
        events.put(("eof", None))

    threading.Thread(target=read_worker_output, name="pdf-worker-output", daemon=True).start()
    finish_result: object | None = None
    progress_events = 0
    last_stage: str | None = None
    stage_started_at = time.monotonic()
    stage_durations: dict[str, float] = {}
    token_usage: object | None = None
    translation_stats: dict[str, int] | None = None
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
                stage = event["stage"]
                now = time.monotonic()
                if last_stage is not None:
                    stage_durations[last_stage] = round(
                        stage_durations.get(last_stage, 0.0) + now - stage_started_at,
                        3,
                    )
                last_stage = stage
                stage_started_at = now
            if event.get("type") == "translation_stats":
                translation_stats = {
                    key: value
                    for key in ("successful_paragraphs", "fallback_paragraphs", "translated_paragraphs")
                    if isinstance((value := event.get(key)), int)
                }
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
            "stage_durations": stage_durations,
        }
        if token_usage is not None:
            metadata["token_usage"] = token_usage
        if translation_stats is not None:
            metadata["babeldoc_translation_stats"] = translation_stats
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
    results = [record for record in records if record.get("event") == "upstream_result"]
    elapsed = [record.get("elapsed_ms") for record in results if isinstance(record.get("elapsed_ms"), (int, float))]
    failures = [record for record in results if record.get("outcome") != "ok"]
    return {
        "gateway_requests": len(requests),
        "gateway_batch_sizes": sizes,
        "gateway_structured_requests": len(sizes),
        "gateway_fallback_requests": len(requests) - len(sizes),
        "gateway_upstream_results": len(results),
        "gateway_upstream_elapsed_ms_total": round(sum(elapsed), 3),
        "gateway_upstream_elapsed_ms_max": round(max(elapsed), 3) if elapsed else 0,
        "gateway_upstream_failures": len(failures),
    }


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
