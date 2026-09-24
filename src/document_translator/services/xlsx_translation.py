"""Safe orchestration for ordinary XLSX cell-string translation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from document_translator.adapters.xlsx import XlsxReadResult, read_xlsx, rewrite_xlsx
from document_translator.core import TranslationResult, TranslationUnit, validate_result_for_unit

from .markdown_translation import UnitTranslationProvider


class XlsxTranslationServiceError(RuntimeError):
    """Raised when XLSX translation cannot create a safe destination."""


@dataclass(frozen=True, slots=True)
class XlsxTranslationOutcome:
    units: tuple[TranslationUnit, ...]
    results: tuple[TranslationResult, ...]
    skipped: tuple[str, ...]


class XlsxTranslationService:
    def __init__(self, provider: UnitTranslationProvider, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._provider = provider
        self._max_attempts = max_attempts

    def translate_file(
        self,
        source_path: str | Path,
        destination_path: str | Path,
        *,
        source_language: str,
        target_language: str,
        include_hidden_sheets: bool = False,
    ) -> XlsxTranslationOutcome:
        source, destination = Path(source_path), Path(destination_path)
        if source.resolve() == destination.resolve():
            raise XlsxTranslationServiceError("source and destination paths must differ")
        try:
            read_result = read_xlsx(
                source,
                source_language=source_language,
                target_language=target_language,
                include_hidden_sheets=include_hidden_sheets,
            )
        except Exception as error:
            raise XlsxTranslationServiceError("unable to extract XLSX text cells") from error
        results = self._translate_units(read_result.units)
        try:
            rewrite_xlsx(
                source,
                destination,
                read_result.units,
                {item.unit_id: item for item in results},
                source_language=source_language,
                target_language=target_language,
                include_hidden_sheets=include_hidden_sheets,
            )
        except Exception as error:
            raise XlsxTranslationServiceError("unable to safely write XLSX destination") from error
        return XlsxTranslationOutcome(
            units=read_result.units,
            results=results,
            skipped=read_result.skipped,
        )

    def _translate_units(self, units: tuple[TranslationUnit, ...]) -> tuple[TranslationResult, ...]:
        batch_translate = getattr(self._provider, "translate_batch", None)
        if callable(batch_translate) and getattr(self._provider, "supports_stable_batch", True):
            try:
                results = tuple(batch_translate(list(units)))
            except Exception as error:
                raise XlsxTranslationServiceError(f"Qwen-MT batch translation failed: {error}") from error
            if len(results) != len(units):
                raise XlsxTranslationServiceError("Qwen-MT batch translation count mismatch")
            for unit, result in zip(units, results, strict=True):
                errors = validate_result_for_unit(unit, result)
                if errors:
                    raise XlsxTranslationServiceError("Qwen-MT batch result is invalid: " + ", ".join(errors))
            return results
        return tuple(self._translate_and_validate(unit) for unit in units)

    def _translate_and_validate(self, unit: TranslationUnit) -> TranslationResult:
        last_error: Exception | None = None
        for _ in range(self._max_attempts):
            try:
                result = self._provider.translate_unit(unit)
                errors = validate_result_for_unit(unit, result)
                if result.provider != self._provider.provider_name:
                    errors.append("PROVIDER_MISMATCH")
                if result.model != self._provider.config.model:
                    errors.append("MODEL_MISMATCH")
                if errors:
                    raise XlsxTranslationServiceError(
                        "translation provider returned an invalid result: " + ", ".join(errors),
                    )
                return result
            except Exception as error:
                last_error = error
        reason = getattr(last_error, "code", None) or type(last_error).__name__
        raise XlsxTranslationServiceError(
            f"translation provider failed after {self._max_attempts} attempts for unit {unit.id}: {reason}",
        ) from last_error
