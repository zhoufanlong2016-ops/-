"""In-process orchestration for deterministic Markdown translation stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from document_translator.adapters.markdown import (
    MarkdownRewriteResult,
    extract_translation_units,
    read_markdown,
    rewrite_markdown,
    write_markdown,
)
from document_translator.core import (
    TranslationResult,
    TranslationUnit,
    validate_result_for_unit,
)

from .cache import TranslationCache


class ProviderConfig(Protocol):
    """The provider configuration field required for cache identity."""

    model: str


class UnitTranslationProvider(Protocol):
    """The single-unit provider surface shared by the local providers."""

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


class MarkdownTranslationService:
    """Coordinate extraction, optional cache lookup, provider calls, and rewrite."""

    def __init__(
        self,
        provider: UnitTranslationProvider,
        cache: TranslationCache | None = None,
        *,
        translation_mode: str = "default",
    ) -> None:
        if not translation_mode:
            raise ValueError("translation_mode must not be empty")
        self._provider = provider
        self._cache = cache
        self._translation_mode = translation_mode

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

        results: list[TranslationResult] = []
        cache_hits = 0
        cache_misses = 0
        for unit in units:
            result = self._cached_result(unit)
            if result is None:
                cache_misses += 1
                result = self._translate_and_validate(unit)
                if self._cache is not None:
                    try:
                        self._cache.put(unit, result, translation_mode=self._translation_mode)
                    except Exception as error:
                        raise MarkdownTranslationServiceError("unable to store translation result in cache") from error
            else:
                cache_hits += 1
            results.append(result)

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
        try:
            result = self._provider.translate_unit(unit)
        except Exception as error:
            raise MarkdownTranslationServiceError("translation provider failed") from error
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
        return result

    def _provider_model(self) -> str:
        try:
            model = self._provider.config.model
        except AttributeError as error:
            raise MarkdownTranslationServiceError("translation provider config must define model") from error
        if not isinstance(model, str) or not model:
            raise MarkdownTranslationServiceError("translation provider config model must be non-empty text")
        return model
