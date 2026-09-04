"""SQLite-backed, explicitly located cache for validated translation results."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import ValidationError

from document_translator.core import (
    TranslationResult,
    TranslationUnit,
    generate_cache_key,
    sha256_text,
    validate_result_for_unit,
)


class TranslationCacheError(RuntimeError):
    """Base error for translation-cache failures."""


class CacheCorruptionError(TranslationCacheError):
    """Raised when a stored cache payload cannot be validated."""


class CacheClosedError(TranslationCacheError):
    """Raised when a closed cache is used."""


class TranslationCache:
    """Persist validated translation results in the caller-selected SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        if str(database_path) != ":memory:":
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(str(database_path))
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
        except sqlite3.DatabaseError as error:
            raise TranslationCacheError("unable to initialize translation cache") from error
        self._closed = False

    def __enter__(self) -> "TranslationCache":
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def get(
        self,
        unit: TranslationUnit,
        *,
        provider: str,
        model: str,
        prompt_version: str,
        glossary_version: str,
        translation_mode: str = "default",
    ) -> TranslationResult | None:
        """Return a validated result for the supplied cache identity, or ``None``."""
        self._require_open()
        cache_key = self._cache_key(
            unit,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            glossary_version=glossary_version,
            translation_mode=translation_mode,
        )
        try:
            row = self._connection.execute(
                "SELECT payload FROM translation_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise TranslationCacheError("unable to read translation cache") from error
        if row is None:
            return None
        try:
            result = TranslationResult.model_validate_json(row[0])
        except (TypeError, ValidationError, ValueError) as error:
            raise CacheCorruptionError("cached translation result is invalid") from error
        source_hash = sha256_text(unit.source_text)
        if result.source_hash != source_hash or (
            result.provider != provider
            or result.model != model
            or result.prompt_version != prompt_version
            or result.glossary_version != glossary_version
        ):
            raise CacheCorruptionError("cached translation result does not match its cache identity")
        rebound_result = TranslationResult.model_validate(
            result.model_dump() | {"unit_id": unit.id, "source_hash": source_hash}
        )
        validation_errors = validate_result_for_unit(unit, rebound_result)
        if validation_errors:
            raise CacheCorruptionError("cached translation result does not match its cache identity")
        return rebound_result

    def put(
        self,
        unit: TranslationUnit,
        result: TranslationResult,
        *,
        translation_mode: str = "default",
    ) -> None:
        """Validate and atomically store ``result`` for ``unit``."""
        self._require_open()
        validation_errors = validate_result_for_unit(unit, result)
        if validation_errors:
            raise TranslationCacheError(
                "translation result does not match translation unit: "
                + ", ".join(validation_errors)
            )
        cache_key = self._cache_key(
            unit,
            provider=result.provider,
            model=result.model,
            prompt_version=result.prompt_version,
            glossary_version=result.glossary_version,
            translation_mode=translation_mode,
        )
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO translation_cache (cache_key, payload) VALUES (?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload
                    """,
                    (cache_key, result.model_dump_json()),
                )
        except sqlite3.DatabaseError as error:
            raise TranslationCacheError("unable to write translation cache") from error

    @staticmethod
    def _cache_key(
        unit: TranslationUnit,
        *,
        provider: str,
        model: str,
        prompt_version: str,
        glossary_version: str,
        translation_mode: str,
    ) -> str:
        # generate_cache_key has no provider argument.  A NUL separator preserves
        # the provider/model boundary while retaining that core key implementation.
        return generate_cache_key(
            source_text=unit.source_text,
            source_language=unit.source_language,
            target_language=unit.target_language,
            model=f"{provider}\0{model}",
            prompt_version=prompt_version,
            glossary_version=glossary_version,
            protected_tokens=unit.protected_tokens,
            translation_mode=translation_mode,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise CacheClosedError("translation cache is closed")
