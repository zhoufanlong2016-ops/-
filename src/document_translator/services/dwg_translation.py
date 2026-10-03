"""Safe orchestration from a CadBridge DWG export to an AutoCAD import task."""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys
import threading
from pathlib import Path
import re
import time
from typing import Mapping

from document_translator.adapters import dwg
from document_translator.core import (
    DocumentFormat,
    DocumentLocation,
    TranslationResult,
    TranslationUnit,
    generate_unit_id,
    sha256_text,
    validate_result_for_unit,
)

from .markdown_translation import UnitTranslationProvider
from document_translator.translation_rules import rule_protected_tokens


_LETTER_RE = re.compile(r"[A-Za-z㐀-鿿豈-﫿]")


# A lowercase English word ("Clogged", "Door"); MText codes are excluded.
_ORDINARY_WORD_RE = re.compile(r"(?<![A-Za-z⟦_])[A-Za-z][a-z]{3,}(?![A-Za-z])")


def _needs_translation(text: str) -> bool:
    return bool(_LETTER_RE.search(text))


def _workers() -> int:
    try:
        return max(1, min(8, int(os.environ.get("DOCUMENT_TRANSLATOR_PDF_WORKERS", "6"))))
    except ValueError:
        return 6


class DwgTranslationServiceError(RuntimeError):
    """Raised when an exported DWG cannot safely become an import task."""


@dataclass(frozen=True, slots=True)
class DwgTranslationOutcome:
    """The validated translations and files needed for the AutoCAD write-back."""

    units: tuple[TranslationUnit, ...]
    results: tuple[TranslationResult, ...]
    import_task: dwg.ImportTask
    task_json: Path
    command_script: Path


class DwgTranslationService:
    """Translate CadBridge text records without reading or modifying DWG bytes."""

    def __init__(self, provider: UnitTranslationProvider, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._provider = provider
        self._max_attempts = max_attempts
        self.warnings: list[dict[str, object]] = []
        self._warnings_lock = threading.Lock()

    def prepare_import(
        self,
        export_json: str | Path,
        *,
        destination_dwg: str | Path,
        task_json: str | Path,
        result_json: str | Path,
        command_script: str | Path,
        source_language: str,
        target_language: str,
        overwrite: bool = False,
        font_policy: Mapping[str, str | Mapping[str, object]] | None = None,
    ) -> DwgTranslationOutcome:
        """Translate one CadBridge export and create a non-overwriting import task.

        The returned script is for an already-loaded CadBridge in AutoCAD.  This
        method never opens AutoCAD and never writes the source DWG.
        """
        try:
            exported = dwg.read_export_document(export_json)
        except Exception as error:
            raise DwgTranslationServiceError("unable to read CadBridge export JSON") from error
        units = tuple(
            self._unit_from_item(exported, item, source_language, target_language)
            for item in exported.items
        )
        results = self._translate_and_validate_batches(units)
        translations = tuple(
            dwg.make_translation(
                item,
                result.translation,
                font_decision=self._font_decision(item, font_policy),
            )
            for item, result in zip(exported.items, results, strict=True)
        )
        try:
            task = dwg.write_import_task(
                task_json,
                source_dwg=exported.source_dwg,
                destination_dwg=destination_dwg,
                export_json=export_json,
                result_json=result_json,
                translations=translations,
                overwrite=overwrite,
            )
            dwg.write_command_script(
                command_script,
                operation="import",
                task_json=task_json,
                overwrite=overwrite,
            )
        except Exception as error:
            raise DwgTranslationServiceError("unable to write CadBridge import task") from error
        return DwgTranslationOutcome(
            units=units,
            results=results,
            import_task=task,
            task_json=Path(task_json).expanduser().resolve(),
            command_script=Path(command_script).expanduser().resolve(),
        )

    @staticmethod
    def _font_decision(
        item: dwg.TextItem,
        font_policy: Mapping[str, str | Mapping[str, object]] | None,
    ) -> dwg.FontDecision | None:
        """Carry an explicit source-style to target-font mapping to CadBridge.

        Character coverage and geometry remain target-application checks; the
        Python layer must not guess a font or silently alter the drawing.
        """
        if not font_policy:
            return None
        target = font_policy.get(item.metadata.style_name) or font_policy.get(item.metadata.style_handle)
        if target is None:
            return None
        if isinstance(target, Mapping):
            try:
                return dwg.FontDecision.model_validate(dict(target), strict=True)
            except ValueError as error:
                raise DwgTranslationServiceError(
                    f"invalid font policy for style {item.metadata.style_name}: {error}",
                ) from error
        target = str(target).strip()
        if not target:
            raise DwgTranslationServiceError(
                f"font policy contains an empty target for style {item.metadata.style_name}",
            )
        return dwg.FontDecision(target_font_file=target)

    def _unit_from_item(
        self,
        exported: dwg.ExportDocument,
        item: dwg.TextItem,
        source_language: str,
        target_language: str,
    ) -> TranslationUnit:
        protected_tokens = rule_protected_tokens(item.source_text, [sequence.token for sequence in item.protected_sequences])
        location = DocumentLocation(
            part=item.space,
            object_id=item.handle,
            node_ids=[item.entity_type, item.layer],
        )
        style_signature = (
            f"{item.entity_type}|{item.space}|{item.layer}|{item.metadata.style_handle}|"
            f"{item.metadata.height}|{item.metadata.width_factor}"
        )
        data = {
            "document_hash": exported.source_sha256,
            "format": DocumentFormat.DWG,
            "location": location,
            "source_language": source_language,
            "target_language": target_language,
            "source_text": item.source_text,
            "protected_tokens": protected_tokens,
            "style_signature": style_signature,
        }
        return TranslationUnit(id=generate_unit_id(**data), **data)

    def _translate_and_validate_batches(
        self, units: tuple[TranslationUnit, ...], *, batch_size: int = 8,
    ) -> tuple[TranslationResult, ...]:
        """Submit bounded batches only; never fall back to one request per item."""
        translate_batch = getattr(self._provider, "translate_batch", None)
        if not callable(translate_batch):
            raise DwgTranslationServiceError("DWG translation provider must support translate_batch")
        if (
            getattr(self._provider, "provider_name", "") == "qwen_mt"
            or not getattr(self._provider, "supports_stable_batch", True)
        ):
            # Qwen-MT is a native plain-text endpoint.  It cannot preserve
            # synthetic stable-ID envelopes, so use its original unit
            # protocol and keep validation around every result.
            return tuple(self._translate_and_validate_unit(unit) for unit in units)
        # A drawing is mostly bare numbers (levels, areas, chainages) and the
        # same label repeated: numbers keep their own text without a request,
        # each distinct text is translated once, and batches run concurrently
        # (one sequential request per 8 items took 4 minutes for 204 items).
        by_text: dict[tuple[str, tuple[str, ...]], TranslationUnit] = {}
        for unit in units:
            if _needs_translation(unit.source_text):
                by_text.setdefault((unit.source_text, tuple(unit.protected_tokens)), unit)
        distinct = list(by_text.values())
        batches = [tuple(distinct[start:start + batch_size]) for start in range(0, len(distinct), batch_size)]
        translated: dict[tuple[str, tuple[str, ...]], str] = {}
        if batches:
            from concurrent.futures import ThreadPoolExecutor

            done = 0
            with ThreadPoolExecutor(max_workers=min(_workers(), len(batches))) as pool:
                for batch, batch_results in zip(
                    batches, pool.map(lambda batch: self._translate_batch_or_items(batch, translate_batch), batches)
                ):
                    done += len(batch)
                    print(f"translation: {done}/{len(distinct)}", file=sys.stderr, flush=True)
                    for unit, result in zip(batch, batch_results, strict=True):
                        if result is not None:
                            translated[(unit.source_text, tuple(unit.protected_tokens))] = result.translation
        results: list[TranslationResult] = []
        for unit in units:
            key = (unit.source_text, tuple(unit.protected_tokens))
            text = translated.get(key, unit.source_text)
            result = TranslationResult(
                unit_id=unit.id,
                translation=text,
                provider=self._provider.provider_name,
                model=self._provider.config.model,
                prompt_version=self._provider.prompt_version,
                glossary_version=self._provider.glossary_version,
                source_hash=sha256_text(unit.source_text),
                result_hash=sha256_text(text),
                request_count=1,
                validation_status="valid",
            )
            # Numbers keep their text without a request, and an item that
            # kept failing keeps its source text and is already in warnings.
            if key in translated:
                self._validate_provider_result(unit, result)
            results.append(result)
        return tuple(results)

    def _translate_batch_or_items(self, units, translate_batch) -> list[TranslationResult | None]:
        """One failing batch must not discard the whole drawing.

        A batch that still fails after its retries (a slow or unreachable
        API: requests timing out) is retried item by item; an item that
        still fails keeps its source text and is reported in ``warnings``.
        """
        try:
            results = list(self._translate_and_validate_batch(units, translate_batch))
        except DwgTranslationServiceError as batch_error:
            results: list[TranslationResult | None] = []
            for unit in units:
                try:
                    results.append(self._translate_and_validate_batch((unit,), translate_batch)[0])
                except DwgTranslationServiceError as error:
                    results.append(None)
                    with self._warnings_lock:
                        self.warnings.append({"object_id": unit.location.object_id, "text": unit.source_text, "errors": [str(error) or str(batch_error)]})
            return results
        # In a batch of codes and names ("MAM", "LW - TDW - 003") a model
        # copied ordinary labels too ("50% Clogged", "Lower Door"); asked on
        # its own it translates them. A label returned unchanged that has a
        # lowercase English word gets that one more request.
        for index, (unit, result) in enumerate(zip(units, results)):
            if result.translation.strip() == unit.source_text.strip() and _ORDINARY_WORD_RE.search(unit.source_text):
                try:
                    retry = self._translate_and_validate_batch((unit,), translate_batch)[0]
                except DwgTranslationServiceError:
                    continue
                results[index] = retry
        return results

    def _translate_and_validate_batch(self, units, translate_batch) -> tuple[TranslationResult, ...]:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                results = tuple(translate_batch(list(units)))
                if len(results) != len(units):
                    raise DwgTranslationServiceError("batch translation count does not match source items")
                for unit, result in zip(units, results, strict=True):
                    self._validate_provider_result(unit, result)
                return results
            except Exception as error:
                last_error = error
                if getattr(error, "code", None) == "HTTP_429" and attempt + 1 < self._max_attempts:
                    time.sleep(2**attempt)
        reason = getattr(last_error, "code", None) or type(last_error).__name__
        raise DwgTranslationServiceError(
            f"batch translation provider failed after {self._max_attempts} attempts for "
            f"{len(units)} item(s): "
            f"{reason}: {last_error}",
        ) from last_error

    def _translate_and_validate_unit(self, unit: TranslationUnit) -> TranslationResult:
        last_error: Exception | None = None
        for _ in range(self._max_attempts):
            try:
                result = self._provider.translate_unit(unit)
                self._validate_provider_result(unit, result)
                return result
            except Exception as error:
                last_error = error
        reason = getattr(last_error, "code", None) or type(last_error).__name__
        raise DwgTranslationServiceError(
            f"translation provider failed after {self._max_attempts} attempts for unit {unit.id}: {reason}"
        ) from last_error

    def _validate_provider_result(self, unit: TranslationUnit, result: object) -> None:
        if not isinstance(result, TranslationResult):
            raise DwgTranslationServiceError("translation provider returned an invalid result type")
        errors = validate_result_for_unit(unit, result)
        if result.provider != self._provider.provider_name:
            errors.append("PROVIDER_MISMATCH")
        if result.model != self._provider.config.model:
            errors.append("MODEL_MISMATCH")
        if result.prompt_version != self._provider.prompt_version:
            errors.append("PROMPT_VERSION_MISMATCH")
        if result.glossary_version != self._provider.glossary_version:
            errors.append("GLOSSARY_VERSION_MISMATCH")
        if errors:
            raise DwgTranslationServiceError(
                "translation provider returned an invalid result: " + ", ".join(errors),
            )
