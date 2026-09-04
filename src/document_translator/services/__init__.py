"""Service-layer utilities for document translation."""

from .cache import (
    CacheClosedError,
    CacheCorruptionError,
    TranslationCache,
    TranslationCacheError,
)
from .markdown_translation import (
    MarkdownTranslationOutcome,
    MarkdownTranslationService,
    MarkdownTranslationServiceError,
    ProviderConfig,
    UnitTranslationProvider,
)
from .glossary import Glossary, GlossaryEntry, GlossaryError, load_glossary

__all__ = [
    "CacheClosedError",
    "CacheCorruptionError",
    "TranslationCache",
    "TranslationCacheError",
    "MarkdownTranslationOutcome",
    "MarkdownTranslationService",
    "MarkdownTranslationServiceError",
    "ProviderConfig",
    "UnitTranslationProvider",
    "Glossary",
    "GlossaryEntry",
    "GlossaryError",
    "load_glossary",
]
