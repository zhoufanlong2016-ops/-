"""Preflight, candidate validation, and auditable publication for PDF jobs.

This module deliberately does not edit PDF page content.  BabelDOC owns layout
reconstruction; PyMuPDF is used only to classify the source and validate a
candidate before it is published.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


PdfClass = Literal["A", "B", "C", "D", "E", "F"]


@dataclass(frozen=True, slots=True)
class PdfPreflight:
    source_hash: str
    page_count: int
    page_sizes: tuple[tuple[float, float], ...]
    text_characters: int
    image_count: int
    rotated_text_spans: int
    classification: PdfClass
    reasons: tuple[str, ...]
    table_pages: tuple[int, ...] = ()
    visual_review_required: bool = False


class PdfPreflightError(RuntimeError):
    """Raised when a PDF cannot safely enter the layout worker."""


# PDF text extracted by different engines occasionally contains C0 control
# characters.  CR/LF/TAB are meaningful layout whitespace and must be kept;
# the remaining C0 range is never valid user-visible PDF text and can break
# BabelDOC's JSON batch parser.
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_UNICODE_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015"
_IMMUTABLE_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d)"
    r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9])"
)
# Chinese legal-document references such as ``中土经营〔2024〕341 号`` are
# immutable identifiers too.  They are allowed to remain in an English
# translation only when the exact source fragment survives unchanged.
_MIXED_IMMUTABLE_IDENTIFIER_RE = re.compile(
    r"[A-Za-z\u3400-\u9fff]{2,}[〔\[\(（]\d{2,4}[〕\]\)）]\s*\d+\s*号"
)


def control_characters(text: str) -> tuple[str, ...]:
    """Return non-whitespace C0 controls in deterministic display form."""
    return tuple(sorted({f"U+{ord(char):04X}" for char in _CONTROL_CHARACTER_RE.findall(text)}))


def remove_control_characters(text: str) -> str:
    """Remove only unsafe C0 controls while preserving CR/LF/TAB."""
    return _CONTROL_CHARACTER_RE.sub("", text)


def normalize_unicode_dashes(text: str) -> str:
    """Use the shared ASCII hyphen policy for model-produced dash variants."""

    return text.translate(str.maketrans({dash: "-" for dash in _UNICODE_DASHES}))


def _font_name_key(value: object) -> str:
    """Return a stable comparison key for a PDF resource/font face name."""
    text = str(value or "").casefold().split("+")[-1]
    if text.endswith(".ttf") or text.endswith(".otf"):
        text = text.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", text)


def repair_pdf_text_cmaps(path: str | Path) -> dict[str, object]:
    """Repair BabelDOC's known synthetic-space ToUnicode mappings.

    BabelDOC's embedded TrueType font subsetting can omit the no-outline
    space glyph.  Depending on the font/cache combination the generated PDF
    exposes that glyph as U+0001 or U+0003.  Mapping only these observed
    synthetic-space codes to U+0020 changes copy/search semantics, not page
    geometry or painted glyphs.  Other control characters remain a hard
    validation error.
    """
    pdf_path = Path(path)
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise PdfPreflightError("PDF CMap repair requires PyMuPDF") from exc

    document = fitz.open(pdf_path)
    repairs: list[dict[str, object]] = []
    try:
        for page in document:
            controls_by_font: dict[str, dict[int, int]] = {}
            raw = page.get_text("rawdict")
            for block in raw.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        chars = span.get("chars", [])
                        counts = {
                            code: sum(1 for char in chars if char.get("c") == chr(code))
                            for code in (0x0001, 0x0003)
                        }
                        counts = {code: count for code, count in counts.items() if count}
                        if counts:
                            key = _font_name_key(span.get("font"))
                            existing = controls_by_font.setdefault(key, {})
                            for code, count in counts.items():
                                existing[code] = existing.get(code, 0) + count
            if not controls_by_font:
                continue

            for font in page.get_fonts(full=True):
                if len(font) < 5:
                    continue
                font_key = _font_name_key(font[3])
                resource_key = _font_name_key(font[4])
                counts = controls_by_font.get(font_key) or controls_by_font.get(resource_key)
                if not counts:
                    continue
                cmap_type, cmap_ref = document.xref_get_key(font[0], "ToUnicode")
                if cmap_type != "xref":
                    continue
                try:
                    cmap_xref = int(str(cmap_ref).split()[0])
                    cmap = document.xref_stream(cmap_xref)
                except (TypeError, ValueError, OSError):
                    continue
                if not cmap:
                    continue
                repaired_cmap = cmap
                font_repairs: list[dict[str, object]] = []
                for code, count in counts.items():
                    mapping = re.compile(rb"<0{0,3}%X>\s*<([0-9a-fA-F]+)>" % code)
                    match = mapping.search(repaired_cmap)
                    if match and int(match.group(1), 16) == 0x20:
                        continue
                    if match:
                        repaired_cmap = mapping.sub(
                            (f"<{code:04X}><0020>").encode("ascii"), repaired_cmap, count=1
                        )
                    elif b"endcmap" in repaired_cmap:
                        repaired_cmap = repaired_cmap.replace(
                            b"endcmap",
                            f"1 beginbfchar\n<{code:04X}><0020>\nendbfchar\nendcmap".encode("ascii"),
                            1,
                        )
                    else:
                        continue
                    font_repairs.append(
                        {
                            "font": str(font[4]),
                            "mapping": f"U+{code:04X}->U+0020",
                            "control_count": count,
                        }
                    )
                if font_repairs:
                    document.update_stream(cmap_xref, repaired_cmap)
                    repairs.extend(font_repairs)

        if not repairs:
            return {"repaired_fonts": 0, "repairs": []}

        fd, temporary_name = tempfile.mkstemp(
            prefix=f"{pdf_path.stem}-cmap-", suffix=".pdf", dir=pdf_path.parent
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            document.save(temporary_path, garbage=3, deflate=True)
            document.close()
            document = None
            os.replace(temporary_path, pdf_path)
        except Exception:
            if document is not None:
                document.close()
                document = None
            temporary_path.unlink(missing_ok=True)
            raise
        return {"repaired_fonts": len(repairs), "repairs": repairs}
    finally:
        if document is not None:
            document.close()


def _is_table_like_page(page: object) -> bool:
    """Detect a conservative grid signature without OCR or content changes."""
    try:
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        drawings = page.get_drawings()
    except Exception:
        return False
    horizontal = 0
    vertical = 0
    for drawing in drawings:
        for item in drawing.get("items", []):
            if not item:
                continue
            if item[0] == "l" and len(item) >= 3:
                first, second = item[1], item[2]
                dx = abs(float(second.x) - float(first.x))
                dy = abs(float(second.y) - float(first.y))
                if dy <= 1.5 and dx >= page_width * 0.35:
                    horizontal += 1
                elif dx <= 1.5 and dy >= page_height * 0.15:
                    vertical += 1
            elif item[0] == "re" and len(item) >= 2:
                rect = item[1]
                width = abs(float(rect.x1) - float(rect.x0))
                height = abs(float(rect.y1) - float(rect.y0))
                if width >= page_width * 0.35 and height <= 2:
                    horizontal += 1
                elif height >= page_height * 0.15 and width <= 2:
                    vertical += 1
    return horizontal >= 3 and vertical >= 2


def extract_immutable_identifiers(text: str) -> tuple[str, ...]:
    """Extract code-like identifiers whose ASCII spelling must survive translation."""
    return tuple(sorted(set(_IMMUTABLE_IDENTIFIER_RE.findall(text))))


def restore_immutable_identifiers(source_text: str, translated_text: str) -> str:
    """Restore source identifier spelling when a model changes dash glyphs.

    The replacement is limited to identifiers found in the source prompt and
    accepts only common Unicode dash variants between the same alphanumeric
    segments.  Ordinary prose punctuation is left untouched.
    """
    restored = translated_text
    dash_class = "[" + re.escape("-" + _UNICODE_DASHES) + "]"
    for identifier in extract_immutable_identifiers(source_text):
        parts = [re.escape(part) for part in identifier.split("-")]
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){dash_class.join(parts)}(?![A-Za-z0-9])"
        )
        restored = pattern.sub(identifier, restored)
    return restored


def extract_mixed_immutable_identifiers(text: str) -> tuple[str, ...]:
    """Extract Chinese legal/code references for residue accounting."""

    # PDF text extraction may insert or remove whitespace between the closing
    # year bracket and the serial number.  Compare a compact canonical form so
    # layout-only line wrapping does not look like an identifier change.
    return tuple(
        sorted(
            {
                re.sub(r"\s+", "", value)
                for value in _MIXED_IMMUTABLE_IDENTIFIER_RE.findall(text)
            }
        )
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pdf(path: str | Path) -> PdfPreflight:
    """Return a conservative A--F classification without modifying *path*."""
    source = Path(path)
    if source.suffix.casefold() != ".pdf":
        raise PdfPreflightError("source must have a .pdf extension")
    if not source.is_file():
        raise FileNotFoundError(source)
    with source.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PdfPreflightError("source does not have a PDF file header")
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise PdfPreflightError("PDF preflight requires PyMuPDF in the PDF worker environment") from exc
    try:
        document = fitz.open(source)
    except Exception as exc:
        raise PdfPreflightError("source PDF cannot be opened") from exc
    try:
        if document.needs_pass:
            return PdfPreflight(sha256_file(source), 0, (), 0, 0, 0, "E", ("encrypted PDF requires a password",))
        page_sizes: list[tuple[float, float]] = []
        characters = images = rotations = drawings = 0
        table_pages: list[int] = []
        for page_index, page in enumerate(document, 1):
            page_sizes.append((round(float(page.rect.width), 2), round(float(page.rect.height), 2)))
            characters += len(page.get_text("text").strip())
            images += len(page.get_images(full=True))
            drawings += len(page.get_drawings())
            if _is_table_like_page(page):
                table_pages.append(page_index)
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    direction = line.get("dir", (1.0, 0.0))
                    if tuple(round(float(value), 3) for value in direction) != (1.0, 0.0):
                        rotations += 1
        reasons: list[str] = []
        if not page_sizes:
            classification: PdfClass = "F"; reasons.append("PDF has no pages")
        elif characters == 0:
            classification = "D"; reasons.append("no reliable text layer")
        elif characters / len(page_sizes) < 25 and drawings > len(page_sizes) * 80:
            classification = "C"; reasons.append("fragmented text with dense vector drawings")
        elif table_pages or rotations or drawings > len(page_sizes) * 30:
            classification = "B"; reasons.append("complex layout requires full visual review")
        else:
            classification = "A"; reasons.append("native text layer is suitable for automatic processing")
        if table_pages:
            reasons.append(
                "table-like grid detected on page(s) "
                + ", ".join(str(page) for page in table_pages)
                + "; table translation is experimental and requires separate visual acceptance"
            )
        return PdfPreflight(
            sha256_file(source),
            len(page_sizes),
            tuple(page_sizes),
            characters,
            images,
            rotations,
            classification,
            tuple(reasons),
            tuple(table_pages),
            classification == "B",
        )
    finally:
        document.close()


def validate_candidate(
    source: str | Path,
    candidate: str | Path,
    preflight: PdfPreflight,
    *,
    target_language: str = "",
    layout_profile: object | None = None,
    minimum_font_size: float = 6.0,
) -> dict[str, object]:
    """Validate user-visible invariants before atomically publishing a candidate."""
    source_path, candidate_path = Path(source), Path(candidate)
    if minimum_font_size <= 0:
        raise ValueError("minimum_font_size must be greater than zero")
    if not candidate_path.is_file() or candidate_path.stat().st_size == 0:
        raise PdfPreflightError("worker did not create a non-empty candidate PDF")
    if sha256_file(source_path) != preflight.source_hash:
        raise PdfPreflightError("source PDF changed while the task was running")
    try:
        import fitz
        candidate_doc = fitz.open(candidate_path)
    except Exception as exc:
        raise PdfPreflightError("candidate PDF cannot be opened") from exc
    try:
        if candidate_doc.page_count != preflight.page_count:
            raise PdfPreflightError("candidate page count differs from source")
        candidate_sizes = tuple((round(float(page.rect.width), 2), round(float(page.rect.height), 2)) for page in candidate_doc)
        if candidate_sizes != preflight.page_sizes:
            raise PdfPreflightError("candidate page sizes differ from source")
        page_text = "\n".join(page.get_text("text") for page in candidate_doc)
        source_doc = fitz.open(source_path)
        try:
            source_text = "\n".join(page.get_text("text") for page in source_doc)
        finally:
            source_doc.close()
        unsafe_controls = control_characters(page_text)
        if unsafe_controls:
            raise PdfPreflightError("candidate text contains control characters: " + ", ".join(unsafe_controls))
        immutable_identifiers = list(extract_immutable_identifiers(source_text))
        mixed_immutable_identifiers = list(extract_mixed_immutable_identifiers(source_text))
        unicode_dashes = tuple(sorted({f"U+{ord(char):04X}" for char in page_text if char in _UNICODE_DASHES and char != "-"}))
        identifier_dash_mismatches: list[str] = []
        dash_class = "[" + re.escape("-" + _UNICODE_DASHES) + "]"
        for identifier in immutable_identifiers:
            parts = [re.escape(part) for part in identifier.split("-")]
            variant_pattern = re.compile(
                rf"(?<![A-Za-z0-9]){dash_class.join(parts)}(?![A-Za-z0-9])"
            )
            for match in variant_pattern.findall(page_text):
                if match != identifier:
                    identifier_dash_mismatches.append(match)
        if identifier_dash_mismatches:
            raise PdfPreflightError(
                "candidate contains non-ASCII dash characters in immutable identifiers: "
                + ", ".join(sorted(set(identifier_dash_mismatches))[:12])
            )
        missing_identifiers = [value for value in immutable_identifiers if value not in page_text]
        candidate_mixed_raw = _MIXED_IMMUTABLE_IDENTIFIER_RE.findall(page_text)
        candidate_mixed_by_compact = {
            re.sub(r"\s+", "", value): value for value in candidate_mixed_raw
        }
        if missing_identifiers:
            raise PdfPreflightError("candidate changed immutable identifiers: " + ", ".join(missing_identifiers[:12]))
        observed_font_sizes: list[float] = []
        for page in candidate_doc:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            try:
                                observed_font_sizes.append(float(span["size"]))
                            except (KeyError, TypeError, ValueError):
                                continue
        minimum_observed_font_size = min(observed_font_sizes) if observed_font_sizes else None
        if (
            minimum_observed_font_size is not None
            and minimum_observed_font_size + 1e-6 < minimum_font_size
        ):
            raise PdfPreflightError(
                "candidate contains text below minimum font size "
                f"{minimum_font_size:g} pt: {minimum_observed_font_size:g} pt"
            )
        latin_targets = {"en", "en-us", "en-gb", "english"}
        # A mixed Chinese/Latin document reference is not an immutable literal:
        # only its numeric identity is protected.  Count every remaining CJK
        # character, including a reference suffix such as ``号``, for a Latin
        # target.  The previous implementation exempted matching numeric
        # signatures and therefore accepted visibly untranslated references.
        cjk_residue = sum(1 for char in page_text if "\u3400" <= char <= "\u9fff")
        if target_language.strip().casefold() in latin_targets and cjk_residue:
            raise PdfPreflightError(f"candidate contains {cjk_residue} CJK characters for an English target")
        # English output follows the shared translation policy of ordinary
        # ASCII hyphens.  For Chinese/other targets, an em/en dash in prose is
        # legitimate; only the immutable-identifier check above is strict.
        # Typographic dashes in ordinary prose are valid English typography.
        # Identifier-level dash changes were already rejected above; retaining
        # this list in the report makes any remaining prose variants visible
        # without failing an otherwise complete translation.
        empty_pages = [number for number, page in enumerate(candidate_doc, 1) if not page.get_text("text").strip() and not page.get_images(full=True) and not page.get_drawings()]
        if empty_pages:
            raise PdfPreflightError(f"candidate has blank pages: {empty_pages}")
        embedded_fonts = 0
        for page in candidate_doc:
            embedded_fonts += sum(1 for font in page.get_fonts(full=True) if len(font) > 3 and font[3])
        try:
            from .pdf_layout import validate_layout_contract

            layout_validation = validate_layout_contract(
                source_path,
                candidate_path,
                target_language=target_language,
                profile=layout_profile,
                minimum_font_size=minimum_font_size,
            )
        except Exception as exc:
            if isinstance(exc, PdfPreflightError):
                raise
            raise PdfPreflightError(f"layout contract validation failed: {exc}") from exc
        if layout_validation.get("status") != "passed":
            failures = layout_validation.get("failures", [])
            raise PdfPreflightError(
                "PDF layout contract validation failed: " + "; ".join(str(item) for item in failures[:12])
            )
        return {
            "candidate_hash": sha256_file(candidate_path),
            "embedded_font_references": embedded_fonts,
            "blank_pages": empty_pages,
            "cjk_residue": cjk_residue,
            "control_characters": list(unsafe_controls),
            "unicode_dashes": list(unicode_dashes),
            "missing_identifiers": missing_identifiers,
            "mixed_document_references": mixed_immutable_identifiers,
            "preserved_mixed_document_references": [
                value for value in mixed_immutable_identifiers if value in candidate_mixed_by_compact
            ],
            "minimum_font_size": minimum_font_size,
            "minimum_observed_font_size": minimum_observed_font_size,
            "visual_review_required": preflight.visual_review_required,
            "layout_contract": layout_validation,
        }
    finally:
        candidate_doc.close()


def write_pdf_report(
    path: str | Path,
    *,
    preflight: PdfPreflight,
    validation: dict[str, object],
    provider: str,
    model: str,
    run: dict[str, object] | None = None,
) -> Path:
    report = Path(path)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        # Class-B output can be published as a candidate for the requested
        # visual review, but it is not an accepted deliverable until that
        # review is recorded.  Native-text class A output keeps the historical
        # accepted status after all automated gates pass.
        "status": "PENDING_VISUAL_REVIEW" if preflight.visual_review_required else "ACCEPTED",
        "preflight": asdict(preflight),
        "validation": validation,
        "provider": provider,
        "model": model,
    }
    if run:
        payload["run"] = run
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def publish_candidate(candidate: str | Path, destination: str | Path) -> Path:
    """Publish without replacing a destination created by another process."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate_path = Path(candidate)
    try:
        # The candidate is created in ``target.parent``.  A hard-link create is
        # atomic and fails if another process wins the destination race; the
        # following unlink only removes the private candidate name.
        os.link(candidate_path, target)
    except FileExistsError:
        raise FileExistsError("destination already exists; choose a new path")
    except OSError as exc:
        raise PdfPreflightError(
            "cannot atomically publish candidate without replacing the destination"
        ) from exc
    candidate_path.unlink()
    return target
