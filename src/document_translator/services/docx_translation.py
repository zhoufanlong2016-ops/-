"""Safe paragraph-priority orchestration for DOCX translation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile

from document_translator.adapters.docx import (
    extract_paragraph_translation_units,
    rewrite_docx_paragraphs,
)
from document_translator.core import (
    DocumentLocation,
    TranslationResult,
    TranslationUnit,
    generate_unit_id,
    sha256_text,
    validate_result_for_unit,
)
from document_translator.translation_rules import rule_protected_tokens

from .markdown_translation import UnitTranslationProvider


class DocxTranslationServiceError(RuntimeError):
    """Raised when a DOCX cannot be safely translated into a new file."""


@dataclass(frozen=True, slots=True)
class DocxTranslationOutcome:
    units: tuple[TranslationUnit, ...]
    results: tuple[TranslationResult, ...]
    quality_issues: tuple[str, ...] = ()


class DocxTranslationService:
    """Translate complete visible paragraphs, retaining original OOXML layout nodes."""

    def __init__(
        self,
        provider: UnitTranslationProvider,
        *,
        max_attempts: int = 3,
        max_segment_chars: int = 160,
        cache=None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_segment_chars < 1:
            raise ValueError("max_segment_chars must be positive")
        self._provider = provider
        self._max_attempts = max_attempts
        self._max_segment_chars = max_segment_chars
        self._cache = cache
        self.warnings: list[dict[str, object]] = []

    def translate_file(
        self,
        source_path: str | Path,
        destination_path: str | Path,
        *,
        source_language: str,
        target_language: str,
    ) -> DocxTranslationOutcome:
        source = Path(source_path)
        destination = Path(destination_path)
        if source.resolve() == destination.resolve():
            raise DocxTranslationServiceError("source and destination paths must differ")
        try:
            units = extract_paragraph_translation_units(
                source, source_language=source_language, target_language=target_language,
            )
        except Exception as error:
            raise DocxTranslationServiceError("unable to extract DOCX paragraph units") from error
        results = self._translate_units(units)
        try:
            rewrite_docx_paragraphs(source, destination, units, {item.unit_id: item for item in results})
        except Exception as error:
            raise DocxTranslationServiceError("unable to safely write DOCX destination") from error
        return DocxTranslationOutcome(
            units=tuple(units), results=results,
            quality_issues=self._quality_issues(results, target_language),
        )

    def _translate_units(self, units: tuple[TranslationUnit, ...]) -> tuple[TranslationResult, ...]:
        batch_translate = getattr(self._provider, "translate_batch", None)
        if callable(batch_translate) and getattr(self._provider, "supports_stable_batch", True):
            from .batch_runner import settle, translate_units

            raw = translate_units(self._provider, units, cache=self._cache)
            settled, self.warnings = settle(self._provider, units, raw, normalize=self._normalize_translation)
            return tuple(settled)
        return tuple(self._translate_and_validate(unit) for unit in units)

    @staticmethod
    def _quality_issues(results: tuple[TranslationResult, ...], target_language: str) -> tuple[str, ...]:
        """Report review-required output defects without discarding the draft."""
        if target_language.casefold() not in {"en", "en-us", "en-gb"}:
            return ()
        text = "\n".join(result.translation for result in results)
        issues: list[str] = []
        if re.search(r"[\u3400-\u9fff]", text):
            issues.append("CJK_RESIDUE")
        if "—" in text:
            issues.append("EM_DASH")
        if "‑" in text:
            issues.append("NONBREAKING_HYPHEN")
        return tuple(issues)

    def _translate_and_validate(self, unit: TranslationUnit) -> TranslationResult:
        last_error: Exception | None = None
        segments = self._segment_unit(unit)
        for _ in range(self._max_attempts):
            try:
                segment_results = [self._translate_one(segment) for segment in segments]
                if len(segment_results) == 1:
                    return segment_results[0]
                translation = " ".join(result.translation.strip() for result in segment_results)
                translation = self._normalize_translation(translation)
                result = TranslationResult(
                    unit_id=unit.id, translation=translation, provider=self._provider.provider_name,
                    model=self._provider.config.model, prompt_version=self._provider.prompt_version,
                    glossary_version=self._provider.glossary_version, source_hash=sha256_text(unit.source_text),
                    result_hash=sha256_text(translation), request_count=len(segment_results), validation_status="valid",
                )
                errors = validate_result_for_unit(unit, result)
                if errors:
                    raise DocxTranslationServiceError(
                        "segmented translation failed validation: " + ", ".join(errors),
                    )
                return result
            except Exception as error:
                last_error = error
        reason = getattr(last_error, "code", None) or type(last_error).__name__
        raise DocxTranslationServiceError(
            f"translation provider failed after {self._max_attempts} attempts for unit {unit.id}: {reason}",
        ) from last_error

    def _translate_one(self, unit: TranslationUnit) -> TranslationResult:
        result = self._provider.translate_unit(unit)
        normalized = self._normalize_translation(result.translation)
        if normalized != result.translation:
            result = result.model_copy(
                update={"translation": normalized, "result_hash": sha256_text(normalized)},
            )
        errors = validate_result_for_unit(unit, result)
        if result.provider != self._provider.provider_name:
            errors.append("PROVIDER_MISMATCH")
        if result.model != self._provider.config.model:
            errors.append("MODEL_MISMATCH")
        if errors:
            raise DocxTranslationServiceError(
                "translation provider returned an invalid result: " + ", ".join(errors),
            )
        return result

    @staticmethod
    def _normalize_translation(text: str) -> str:
        """Keep model punctuation compatible with the source document style."""
        return text.replace("\u2011", "-").replace("\u2014", "-")

    def _segment_unit(self, unit: TranslationUnit) -> tuple[TranslationUnit, ...]:
        """Split only long source paragraphs at original sentence boundaries.

        Every source segment must yield a validated result; a failed segment
        restarts the complete paragraph and cannot be silently omitted.
        """
        if len(unit.source_text) <= self._max_segment_chars:
            return (unit,)
        sentences = [part for part in re.split(r"(?<=[。！？；])", unit.source_text) if part]
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) > self._max_segment_chars:
                chunks.append(current)
                current = sentence
            else:
                current += sentence
        if current:
            chunks.append(current)
        if len(chunks) <= 1:
            return (unit,)
        segments: list[TranslationUnit] = []
        for index, text in enumerate(chunks, start=1):
            data = unit.model_dump(exclude={"id", "status"})
            data["location"] = DocumentLocation(
                part=unit.location.part,
                object_id=f"{unit.location.object_id}:segment:{index}",
                node_ids=unit.location.node_ids,
            )
            data["source_text"] = text
            data["protected_tokens"] = rule_protected_tokens(text)
            data["id"] = generate_unit_id(**data)
            segments.append(TranslationUnit.model_validate(data))
        return tuple(segments)


def write_docx_comparison_report(path: str | Path, outcome: DocxTranslationOutcome) -> None:
    """Write a non-overwriting UTF-8 audit sidecar for one completed DOCX run."""
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"comparison report already exists: {destination}")
    if not destination.parent.is_dir():
        raise DocxTranslationServiceError(f"comparison report directory does not exist: {destination.parent}")
    records = []
    for ordinal, (unit, result) in enumerate(zip(outcome.units, outcome.results, strict=True), start=1):
        records.append({
            "ordinal": ordinal,
            "unit_id": unit.id,
            "part": unit.location.part,
            "location": unit.location.object_id,
            "source_text": unit.source_text,
            "translation": result.translation,
            "source_hash": result.source_hash,
            "result_hash": result.result_hash,
            "provider": result.provider,
            "model": result.model,
        })
    payload = {
        "format": "document-translator-docx-comparison-v1",
        "document_hash": outcome.units[0].document_hash if outcome.units else None,
        "quality_issues": list(outcome.quality_issues),
        "records": records,
    }
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        Path(temporary_name).replace(destination)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
