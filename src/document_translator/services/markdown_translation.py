"""In-process orchestration for deterministic Markdown translation stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from document_translator.adapters.markdown import (
    MarkdownRewriteResult,
    detect_hard_wraps,
    extract_translation_units,
    read_markdown,
    rewrite_markdown,
    write_markdown,
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

from .cache import TranslationCache


class ProviderConfig(Protocol):
    """The provider configuration field required for cache identity."""

    model: str


class UnitTranslationProvider(Protocol):
    """The single-unit provider surface shared by translation providers."""

    provider_name: str
    prompt_version: str
    glossary_version: str
    config: ProviderConfig

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        """Return one validated translation result for ``unit``."""


class MarkdownTranslationServiceError(RuntimeError):
    """Raised when Markdown translation cannot produce a safe complete result."""


@dataclass(frozen=True, slots=True)
class MarkdownTranslationOutcome:
    """Immutable result of translating and strictly rewriting one Markdown text."""

    units: tuple[TranslationUnit, ...]
    results: tuple[TranslationResult, ...]
    cache_hits: int
    cache_misses: int
    rewrite: MarkdownRewriteResult
    preflight_warnings: tuple[str, ...] = ()


class MarkdownTranslationService:
    """Coordinate extraction, optional cache lookup, provider calls, and rewrite."""

    def __init__(
        self,
        provider: UnitTranslationProvider,
        cache: TranslationCache | None = None,
        *,
        translation_mode: str = "default",
        max_attempts: int = 3,
        max_segment_chars: int | None = None,
    ) -> None:
        if not translation_mode:
            raise ValueError("translation_mode must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_segment_chars is not None and max_segment_chars < 1:
            raise ValueError("max_segment_chars must be positive when set")
        self._provider = provider
        self._cache = cache
        self._translation_mode = translation_mode
        self._max_attempts = max_attempts
        self._max_segment_chars = max_segment_chars

    def translate_text(
        self,
        text: str,
        *,
        source_language: str = "auto",
        target_language: str = "en",
    ) -> MarkdownTranslationOutcome:
        """Translate all extracted units in source order without performing file I/O."""
        try:
            units = extract_translation_units(
                text,
                source_language=source_language,
                target_language=target_language,
            )
        except Exception as error:
            raise MarkdownTranslationServiceError("unable to extract Markdown translation units") from error

        preflight_warnings = detect_hard_wraps(text)
        cache_hits = 0
        cache_misses = 0
        cached: dict[str, TranslationResult] = {}
        uncached: list[TranslationUnit] = []
        for unit in units:
            result = self._cached_result(unit)
            if result is None:
                cache_misses += 1
                uncached.append(unit)
            else:
                cache_hits += 1
                cached[unit.id] = result

        batch_translate = getattr(self._provider, "translate_batch", None)
        if uncached and callable(batch_translate) and getattr(self._provider, "supports_stable_batch", True):
            try:
                batch_results = list(batch_translate(uncached))
            except Exception as error:
                raise MarkdownTranslationServiceError(
                    f"{self._provider.provider_name} batch translation failed",
                ) from error
            if len(batch_results) != len(uncached):
                raise MarkdownTranslationServiceError(
                    f"{self._provider.provider_name} batch translation count mismatch",
                )
            translated = zip(uncached, batch_results, strict=True)
        else:
            translated = ((unit, self._translate_and_validate(unit)) for unit in uncached)
        for unit, result in translated:
            self._validate_provider_result(unit, result)
            cached[unit.id] = result
            if self._cache is not None:
                try:
                    self._cache.put(unit, result, translation_mode=self._translation_mode)
                except Exception as error:
                    raise MarkdownTranslationServiceError("unable to store translation result in cache") from error
        results = [cached[unit.id] for unit in units]

        try:
            rewrite = rewrite_markdown(text, units, {result.unit_id: result for result in results})
        except Exception as error:
            raise MarkdownTranslationServiceError("unable to rewrite Markdown") from error
        if rewrite.errors:
            raise MarkdownTranslationServiceError(
                "Markdown rewrite rejected translation results: " + "; ".join(rewrite.errors)
            )
        return MarkdownTranslationOutcome(
            units=tuple(units),
            results=tuple(results),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            rewrite=rewrite,
            preflight_warnings=preflight_warnings,
        )

    def translate_file(
        self,
        source_path: str | Path,
        destination_path: str | Path,
        *,
        source_language: str = "auto",
        target_language: str = "en",
        overwrite: bool = False,
    ) -> MarkdownTranslationOutcome:
        """Translate a source file and write only the caller-selected destination."""
        source = Path(source_path)
        destination = Path(destination_path)
        if source.resolve() == destination.resolve():
            raise MarkdownTranslationServiceError("source and destination paths must differ")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite existing file: {destination}")
        try:
            loaded = read_markdown(source)
        except Exception as error:
            raise MarkdownTranslationServiceError("unable to read Markdown source") from error
        outcome = self.translate_text(
            loaded.text,
            source_language=source_language,
            target_language=target_language,
        )
        try:
            write_markdown(destination, outcome.rewrite.text, encoding=loaded.encoding, overwrite=overwrite)
        except Exception as error:
            raise MarkdownTranslationServiceError("unable to write Markdown destination") from error
        return outcome

    def _cached_result(self, unit: TranslationUnit) -> TranslationResult | None:
        if self._cache is None:
            return None
        try:
            return self._cache.get(
                unit,
                provider=self._provider.provider_name,
                model=self._provider_model(),
                prompt_version=self._provider.prompt_version,
                glossary_version=self._provider.glossary_version,
                translation_mode=self._translation_mode,
            )
        except Exception as error:
            raise MarkdownTranslationServiceError("unable to read translation result from cache") from error

    def _translate_and_validate(self, unit: TranslationUnit) -> TranslationResult:
        last_error: Exception | None = None
        segments = self._segment_unit(unit)
        for _ in range(self._max_attempts):
            try:
                segment_results = [self._translate_one(segment) for segment in segments]
                if len(segment_results) == 1:
                    return segment_results[0]
                translation = " ".join(item.translation.strip() for item in segment_results)
                translation = self._normalize_translation(translation)
                result = TranslationResult(
                    unit_id=unit.id, translation=translation,
                    provider=self._provider.provider_name, model=self._provider_model(),
                    prompt_version=self._provider.prompt_version, glossary_version=self._provider.glossary_version,
                    source_hash=sha256_text(unit.source_text), result_hash=sha256_text(translation),
                    request_count=sum(item.request_count for item in segment_results), validation_status="valid",
                )
                self._validate_provider_result(unit, result)
                return result
            except Exception as error:
                last_error = error
        reason = getattr(last_error, "code", None) or type(last_error).__name__
        raise MarkdownTranslationServiceError(
            f"translation provider failed after {self._max_attempts} attempts "
            f"for unit {unit.id}: {reason}"
        ) from last_error

    def _translate_one(self, unit: TranslationUnit) -> TranslationResult:
        result = self._provider.translate_unit(unit)
        normalized = self._normalize_translation(result.translation)
        if normalized != result.translation:
            result = result.model_copy(
                update={"translation": normalized, "result_hash": sha256_text(normalized)},
            )
        self._validate_provider_result(unit, result)
        return result

    @staticmethod
    def _normalize_translation(text: str) -> str:
        """Avoid model-introduced non-source line-breaking punctuation."""
        return text.replace("\u2011", "-").replace("\u2014", "-")

    def _segment_unit(self, unit: TranslationUnit) -> tuple[TranslationUnit, ...]:
        """Split configured long Markdown units only at original sentence ends."""
        if self._max_segment_chars is None or len(unit.source_text) <= self._max_segment_chars:
            return (unit,)
        # Qwen-MT may omit a protected literal from a very long prose request.
        # Markdown sources can be English, so split at both Chinese and English
        # sentence boundaries before the provider call.
        sentences = [
            part for part in re.split(r"(?<=[。！？；])|(?<=[.!?;])\s+", unit.source_text) if part
        ]
        bounded_sentences: list[str] = []
        for sentence in sentences:
            while len(sentence) > self._max_segment_chars:
                split_at = sentence.rfind(" ", 0, self._max_segment_chars + 1)
                if split_at <= 0:
                    split_at = self._max_segment_chars
                bounded_sentences.append(sentence[:split_at].rstrip())
                sentence = sentence[split_at:].lstrip()
            if sentence:
                bounded_sentences.append(sentence)
        chunks: list[str] = []
        current = ""
        for sentence in bounded_sentences:
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
            data["protected_tokens"] = rule_protected_tokens(
                text, [token for token in unit.protected_tokens if token in text],
            )
            data["id"] = generate_unit_id(**data)
            segments.append(TranslationUnit.model_validate(data))
        return tuple(segments)

    def _validate_provider_result(self, unit: TranslationUnit, result: object) -> None:
        if not isinstance(result, TranslationResult):
            raise MarkdownTranslationServiceError("translation provider returned an invalid result type")
        identity_errors = []
        if result.provider != self._provider.provider_name:
            identity_errors.append("PROVIDER_MISMATCH")
        if result.model != self._provider_model():
            identity_errors.append("MODEL_MISMATCH")
        if result.prompt_version != self._provider.prompt_version:
            identity_errors.append("PROMPT_VERSION_MISMATCH")
        if result.glossary_version != self._provider.glossary_version:
            identity_errors.append("GLOSSARY_VERSION_MISMATCH")
        errors = [*identity_errors, *validate_result_for_unit(unit, result)]
        if errors:
            raise MarkdownTranslationServiceError(
                "translation provider returned an invalid result: " + ", ".join(errors)
            )

    def _provider_model(self) -> str:
        try:
            model = self._provider.config.model
        except AttributeError as error:
            raise MarkdownTranslationServiceError("translation provider config must define model") from error
        if not isinstance(model, str) or not model:
            raise MarkdownTranslationServiceError("translation provider config model must be non-empty text")
        return model
