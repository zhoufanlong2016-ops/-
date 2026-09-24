"""Strict, JSON-compatible models for the translation core."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DocumentFormat(StrEnum):
    MD = "md"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    PDF = "pdf"
    DWG = "dwg"


class UnitStatus(StrEnum):
    PENDING = "pending"
    TRANSLATED = "translated"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class DocumentLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part: str = Field(min_length=1)
    object_id: str | None = None
    node_ids: list[str] = Field(default_factory=list)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unit_identity(unit_data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "document_hash": unit_data["document_hash"],
        "format": str(unit_data["format"]),
        "location": unit_data["location"],
        "source_language": unit_data["source_language"],
        "target_language": unit_data["target_language"],
        "source_text": unit_data["source_text"],
        "protected_tokens": list(unit_data["protected_tokens"]),
        "style_signature": unit_data["style_signature"],
        "context_before": unit_data["context_before"],
        "context_after": unit_data["context_after"],
    }


def generate_unit_id(*, document_hash: str, format: DocumentFormat | str,
                     location: DocumentLocation | Mapping[str, Any], source_language: str,
                     target_language: str, source_text: str, protected_tokens: Sequence[str] = (),
                     style_signature: str = "", context_before: str = "",
                     context_after: str = "") -> str:
    data = _unit_identity({
        "document_hash": document_hash,
        "format": format,
        "location": location.model_dump(mode="json") if isinstance(location, DocumentLocation) else dict(location),
        "source_language": source_language,
        "target_language": target_language,
        "source_text": source_text,
        "protected_tokens": list(protected_tokens),
        "style_signature": style_signature,
        "context_before": context_before,
        "context_after": context_after,
    })
    return sha256_text(normalized_json(data))


def generate_cache_key(*, source_text: str, source_language: str, target_language: str,
                       model: str, prompt_version: str, glossary_version: str,
                       protected_tokens: Sequence[str] = (), translation_mode: str = "default") -> str:
    return sha256_text(normalized_json({
        "source_text": source_text, "source_language": source_language,
        "target_language": target_language, "model": model,
        "prompt_version": prompt_version, "glossary_version": glossary_version,
        "protected_tokens": list(protected_tokens), "translation_mode": translation_mode,
    }))


class TranslationUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    document_hash: str
    format: DocumentFormat
    location: DocumentLocation
    source_language: str = Field(min_length=1)
    target_language: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    protected_tokens: list[str] = Field(default_factory=list)
    style_signature: str = ""
    context_before: str = ""
    context_after: str = ""
    status: UnitStatus = UnitStatus.PENDING

    @field_validator("id", "document_hash")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("must be a lower-case SHA-256 hex digest")
        return value

    @field_validator("protected_tokens")
    @classmethod
    def validate_token_list(cls, value: list[str]) -> list[str]:
        if any(not token for token in value):
            raise ValueError("protected_tokens must not contain empty values")
        if len(value) != len(set(value)):
            raise ValueError("protected_tokens must be unique")
        return value

    @model_validator(mode="after")
    def validate_stable_id(self) -> "TranslationUnit":
        expected = generate_unit_id(**self.model_dump(exclude={"id", "status"}))
        if self.id != expected:
            raise ValueError("id does not match the stable unit identity")
        return self


class TranslationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str
    translation: str
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    glossary_version: str = Field(min_length=1)
    source_hash: str
    result_hash: str
    request_count: int = Field(ge=1)
    error: str | None = None
    validation_status: str = Field(min_length=1)

    @field_validator("unit_id", "source_hash", "result_hash")
    @classmethod
    def validate_hash_fields(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("must be a lower-case SHA-256 hex digest")
        return value
