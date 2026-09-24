"""Core translation data models and deterministic validation helpers."""

from .models import (
    DocumentFormat, DocumentLocation, TranslationResult, TranslationUnit,
    UnitStatus, generate_cache_key, generate_unit_id, normalized_json, sha256_text,
)
from .validation import validate_glossary_terms, validate_placeholders, validate_result_for_unit

__all__ = [
    "DocumentFormat", "DocumentLocation", "TranslationResult", "TranslationUnit",
    "UnitStatus", "generate_cache_key", "generate_unit_id", "normalized_json",
    "sha256_text", "validate_glossary_terms", "validate_placeholders", "validate_result_for_unit",
]
