import sqlite3

import pytest

from document_translator.core import (
    DocumentFormat,
    DocumentLocation,
    TranslationResult,
    TranslationUnit,
    generate_unit_id,
    sha256_text,
    validate_result_for_unit,
)
from document_translator.services import (
    CacheClosedError,
    CacheCorruptionError,
    TranslationCache,
    TranslationCacheError,
)


def make_unit(**changes: object) -> TranslationUnit:
    data: dict[str, object] = {
        "document_hash": "a" * 64,
        "format": DocumentFormat.MD,
        "location": DocumentLocation(part="input.md"),
        "source_language": "en",
        "target_language": "zh-CN",
        "source_text": "Keep {{name}}.",
        "protected_tokens": ["{{name}}"],
    }
    data.update(changes)
    data["id"] = generate_unit_id(**data)
    return TranslationUnit.model_validate(data)


def make_result(unit: TranslationUnit, translation: str = "保留 {{name}}。", **changes: object) -> TranslationResult:
    data: dict[str, object] = {
        "unit_id": unit.id,
        "translation": translation,
        "provider": "provider-a",
        "model": "model-a",
        "prompt_version": "prompt-1",
        "glossary_version": "glossary-1",
        "source_hash": sha256_text(unit.source_text),
        "result_hash": sha256_text(translation),
        "request_count": 1,
        "validation_status": "valid",
    }
    data.update(changes)
    return TranslationResult.model_validate(data)


def get_cached(cache: TranslationCache, unit: TranslationUnit, result: TranslationResult, **changes: str) -> TranslationResult | None:
    identity = {
        "provider": result.provider,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "glossary_version": result.glossary_version,
        "translation_mode": "default",
    }
    identity.update(changes)
    return cache.get(unit, **identity)


def test_cache_hit_miss_and_pydantic_round_trip(tmp_path) -> None:
    unit = make_unit()
    result = make_result(unit)
    with TranslationCache(tmp_path / "nested" / "cache.sqlite3") as cache:
        assert get_cached(cache, unit, result) is None
        cache.put(unit, result)
        assert get_cached(cache, unit, result) == result


def test_cache_hit_rebinds_result_for_equivalent_unit_with_different_id(tmp_path) -> None:
    unit_a = make_unit()
    unit_b = make_unit(document_hash="b" * 64)
    result_a = make_result(unit_a)
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        cache.put(unit_a, result_a)
        returned = get_cached(cache, unit_b, result_a)

    assert returned is not None
    assert returned.unit_id == unit_b.id
    assert validate_result_for_unit(unit_b, returned) == []


@pytest.mark.parametrize(
    ("unit_changes", "identity_changes"),
    [
        ({}, {"provider": "provider-b"}),
        ({}, {"model": "model-b"}),
        ({}, {"prompt_version": "prompt-2"}),
        ({}, {"glossary_version": "glossary-2"}),
        ({"source_text": "Different {{name}}."}, {}),
        ({}, {"translation_mode": "formal"}),
    ],
)
def test_cache_key_separates_identity_fields(tmp_path, unit_changes, identity_changes) -> None:
    unit = make_unit()
    result = make_result(unit)
    changed_unit = make_unit(**unit_changes)
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        cache.put(unit, result)
        assert get_cached(cache, changed_unit, result, **identity_changes) is None


def test_repeat_put_replaces_prior_value(tmp_path) -> None:
    unit = make_unit()
    first = make_result(unit, "第一版 {{name}}。")
    replacement = make_result(unit, "第二版 {{name}}。", request_count=2)
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        cache.put(unit, first)
        cache.put(unit, replacement)
        assert get_cached(cache, unit, replacement) == replacement


def test_invalid_result_is_rejected_without_persisting(tmp_path) -> None:
    unit = make_unit()
    result = make_result(unit)
    invalid = result.model_copy(update={"source_hash": "b" * 64})
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        with pytest.raises(TranslationCacheError, match="SOURCE_HASH_MISMATCH"):
            cache.put(unit, invalid)
        assert get_cached(cache, unit, result) is None


def test_corrupt_payload_raises_cache_specific_error(tmp_path) -> None:
    unit = make_unit()
    result = make_result(unit)
    path = tmp_path / "cache.sqlite3"
    with TranslationCache(path) as cache:
        cache.put(unit, result)
        cache._connection.execute("UPDATE translation_cache SET payload = ?", ("not-json",))
        cache._connection.commit()
        with pytest.raises(CacheCorruptionError, match="cached translation result is invalid"):
            get_cached(cache, unit, result)


def test_valid_json_with_mismatched_result_is_treated_as_corrupt(tmp_path) -> None:
    unit = make_unit()
    result = make_result(unit)
    path = tmp_path / "cache.sqlite3"
    with TranslationCache(path) as cache:
        cache.put(unit, result)
        mismatched = make_result(unit, provider="other-provider")
        cache._connection.execute(
            "UPDATE translation_cache SET payload = ?", (mismatched.model_dump_json(),)
        )
        cache._connection.commit()
        with pytest.raises(CacheCorruptionError, match="cache identity"):
            get_cached(cache, unit, result)


def test_valid_json_with_wrong_source_hash_is_treated_as_corrupt(tmp_path) -> None:
    unit = make_unit()
    result = make_result(unit)
    path = tmp_path / "cache.sqlite3"
    with TranslationCache(path) as cache:
        cache.put(unit, result)
        corrupted = result.model_copy(update={"source_hash": "b" * 64})
        cache._connection.execute(
            "UPDATE translation_cache SET payload = ?", (corrupted.model_dump_json(),)
        )
        cache._connection.commit()
        with pytest.raises(CacheCorruptionError, match="cache identity"):
            get_cached(cache, unit, result)


def test_context_manager_and_close_prevent_further_use(tmp_path) -> None:
    path = tmp_path / "cache.sqlite3"
    with TranslationCache(path) as cache:
        assert path.exists()
    with pytest.raises(CacheClosedError):
        cache.close()
        cache.get(
            make_unit(), provider="provider-a", model="model-a",
            prompt_version="prompt-1", glossary_version="glossary-1",
        )
