from dataclasses import dataclass

import pytest

import document_translator.services.markdown_translation as markdown_translation
from document_translator.adapters.markdown import MarkdownRewriteResult
from document_translator.core import TranslationResult, TranslationUnit, sha256_text
from document_translator.services import (
    MarkdownTranslationService,
    MarkdownTranslationServiceError,
    TranslationCache,
)


@dataclass(frozen=True)
class FakeConfig:
    model: str = "fake-model"


class FakeProvider:
    provider_name = "fake"
    prompt_version = "fake-prompt-v1"
    glossary_version = "fake-glossary-v1"
    config = FakeConfig()

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("fake provider failed")
        translation = unit.source_text.replace("Hello", "Nihao").replace("world", "shijie")
        return TranslationResult(
            unit_id=unit.id,
            translation=translation,
            provider=self.provider_name,
            model=self.config.model,
            prompt_version=self.prompt_version,
            glossary_version=self.glossary_version,
            source_hash=sha256_text(unit.source_text),
            result_hash=sha256_text(translation),
            request_count=1,
            validation_status="valid",
        )


class FlakyProvider(FakeProvider):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary provider failure")
        return super().translate_unit(unit)


class RecordingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.source_texts: list[str] = []

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        self.source_texts.append(unit.source_text)
        return super().translate_unit(unit)


class BatchFakeProvider(FakeProvider):
    provider_name = "qwen_mt"

    def __init__(self) -> None:
        super().__init__()
        self.batch_calls = 0

    def translate_batch(self, units: list[TranslationUnit]) -> list[TranslationResult]:
        self.batch_calls += 1
        return [self.translate_unit(unit) for unit in units]


def test_in_memory_translates_protected_markdown_and_validates_writeback() -> None:
    provider = FakeProvider()
    outcome = MarkdownTranslationService(provider).translate_text("Hello `code` world\n")

    assert provider.calls == 1
    assert outcome.cache_hits == 0 and outcome.cache_misses == 1
    assert outcome.rewrite.errors == ()
    assert outcome.rewrite.text == "Nihao `code` shijie\n"
    assert outcome.results[0].unit_id == outcome.units[0].id
    assert outcome.preflight_warnings == ()


def test_markdown_units_protect_engineering_values_in_addition_to_inline_syntax() -> None:
    outcome = MarkdownTranslationService(FakeProvider()).translate_text(
        "Hello `code` at 105+820, ISO 9001 and 5%\n",
    )

    assert outcome.units[0].protected_tokens == ["⟦MD_0001⟧", "105+820", "ISO 9001", "5%"]


def test_preflight_warning_is_returned_for_hard_wrap() -> None:
    text = "这是一个足够长但没有句末标点的中文段落，用于确认翻译器能识别跨空行拆开的词语，并且保留源代码中的逐行位置以便安全回写外部承\n\n包商围绕在其身边。\n"
    outcome = MarkdownTranslationService(FakeProvider()).translate_text(text)
    assert outcome.preflight_warnings


def test_second_equivalent_run_uses_cache_without_provider_calls(tmp_path) -> None:
    provider = FakeProvider()
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        service = MarkdownTranslationService(provider, cache)
        first = service.translate_text("Hello world\nHello again\n")
        second = service.translate_text("Hello world\nHello again\n")

    assert first.cache_misses == 2 and first.cache_hits == 0
    assert second.cache_hits == 2 and second.cache_misses == 0
    assert provider.calls == 2


def test_uncached_markdown_units_use_batch_and_then_cache(tmp_path) -> None:
    provider = BatchFakeProvider()
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        service = MarkdownTranslationService(provider, cache)
        first = service.translate_text("Hello world\nHello again\n")
        second = service.translate_text("Hello world\nHello again\n")

    assert first.cache_hits == 0 and first.cache_misses == 2
    assert second.cache_hits == 2 and second.cache_misses == 0
    assert provider.batch_calls == 1


def test_cache_reuses_text_for_different_document_identity(tmp_path) -> None:
    provider = FakeProvider()
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        service = MarkdownTranslationService(provider, cache)
        first = service.translate_text("Hello world\n")
        second = service.translate_text("# Heading\n\nHello world\n")

    assert provider.calls == 2  # Heading is new; the shared body is a cache hit.
    assert second.cache_hits == 1 and second.cache_misses == 1
    body = second.results[1]
    assert body.unit_id == second.units[1].id
    assert body.translation == "Nihao shijie"


def test_file_translation_preserves_source_and_utf8_sig_and_refuses_unsafe_paths(tmp_path) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "translated.md"
    source_bytes = b"\xef\xbb\xbfHello world\n"
    source.write_bytes(source_bytes)
    service = MarkdownTranslationService(FakeProvider())

    outcome = service.translate_file(source, destination)

    assert outcome.rewrite.text == "Nihao shijie\n"
    assert source.read_bytes() == source_bytes
    assert destination.read_bytes() == b"\xef\xbb\xbfNihao shijie\n"
    with pytest.raises(FileExistsError):
        service.translate_file(source, destination)
    with pytest.raises(MarkdownTranslationServiceError, match="must differ"):
        service.translate_file(source, source)


def test_provider_failure_does_not_create_destination(tmp_path) -> None:
    source = tmp_path / "source.md"
    source.write_text("Hello `code` world\n", encoding="utf-8")
    provider_destination = tmp_path / "provider-failure.md"

    with pytest.raises(MarkdownTranslationServiceError, match="provider failed"):
        MarkdownTranslationService(FakeProvider(fail=True)).translate_file(source, provider_destination)

    assert not provider_destination.exists()


def test_retries_an_uncached_unit_and_caches_the_success(tmp_path) -> None:
    provider = FlakyProvider(failures=2)
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        first = MarkdownTranslationService(provider, cache, max_attempts=3).translate_text("Hello world\n")
        second = MarkdownTranslationService(provider, cache, max_attempts=3).translate_text("Hello world\n")

    assert provider.calls == 4
    assert first.cache_misses == 1 and second.cache_hits == 1


def test_retry_exhaustion_does_not_create_destination(tmp_path) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "translated.md"
    source.write_text("Hello world\n", encoding="utf-8")

    with pytest.raises(MarkdownTranslationServiceError, match=r"after 3 attempts for unit [0-9a-f]{64}: RuntimeError"):
        MarkdownTranslationService(FlakyProvider(failures=3), max_attempts=3).translate_file(source, destination)

    assert not destination.exists()


def test_rejects_an_invalid_retry_limit() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        MarkdownTranslationService(FakeProvider(), max_attempts=0)


def test_long_markdown_unit_is_split_at_source_sentence_boundaries() -> None:
    provider = RecordingProvider()
    text = "甲" * 100 + "。" + "乙" * 61 + "。\n"

    outcome = MarkdownTranslationService(provider, max_segment_chars=160).translate_text(text)

    assert provider.source_texts == ["甲" * 100 + "。", "乙" * 61 + "。"]
    assert outcome.results[0].request_count == 2
    assert outcome.rewrite.text == "甲" * 100 + "。 " + "乙" * 61 + "。\n"


def test_rewrite_rejection_does_not_create_destination(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "rewrite-rejected.md"
    source.write_text("Hello world\n", encoding="utf-8")

    monkeypatch.setattr(
        markdown_translation,
        "rewrite_markdown",
        lambda text, units, translations: MarkdownRewriteResult(
            text="Nihao shijie\n",
            errors=("REWRITE_FAILED",),
            replaced_unit_ids=(),
        ),
    )
    with pytest.raises(MarkdownTranslationServiceError, match="Markdown rewrite rejected"):
        MarkdownTranslationService(FakeProvider()).translate_file(source, destination)

    assert not destination.exists()
