from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass

import pytest

import document_translator.__main__ as cli


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None


@dataclass
class FakeOutcome:
    units: tuple[object, ...] = (object(), object())
    cache_hits: int = 1
    cache_misses: int = 1
    preflight_warnings: tuple[str, ...] = ()


class FakeService:
    instances: list["FakeService"] = []
    fail = False

    def __init__(self, provider, cache, *, translation_mode, max_attempts, max_segment_chars=None):
        self.provider = provider
        self.cache = cache
        self.translation_mode = translation_mode
        self.max_attempts = max_attempts
        self.max_segment_chars = max_segment_chars
        self.calls = []
        self.__class__.instances.append(self)

    def translate_file(self, source, destination, **kwargs):
        self.calls.append((source, destination, kwargs))
        if self.__class__.fail:
            raise RuntimeError("provider unavailable")
        destination.write_text("translated", encoding="utf-8")
        return FakeOutcome()


class FakeCache:
    instances: list["FakeCache"] = []

    def __init__(self, path):
        self.path = path
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True


def configure_fakes(monkeypatch):
    FakeService.instances = []
    FakeService.fail = False
    FakeCache.instances = []
    monkeypatch.setattr(cli.httpx, "Client", FakeClient)
    monkeypatch.setattr(cli, "MarkdownTranslationService", FakeService)
    monkeypatch.setattr(cli, "TranslationCache", FakeCache)


def test_qwen_default_factory_and_success_summary(tmp_path, monkeypatch, capsys):
    configure_fakes(monkeypatch)
    source = tmp_path / "source.md"
    destination = tmp_path / "translated.md"
    source.write_text("Hello", encoding="utf-8")

    assert cli.main(["translate-markdown", str(source), str(destination), "--source-language", "en", "--target-language", "zh"]) == 0

    service = FakeService.instances[0]
    # The default is Qwen Chat on the low-cost qwen3.8-flash model; Qwen-MT
    # is still available but only when asked for explicitly.
    assert service.provider.__class__.__name__ == "QwenChatProvider"
    assert service.provider.config.model == "qwen3.8-flash"
    assert service.cache is None
    assert service.max_attempts == 3
    assert "translated 2 units; cache hits=1; cache misses=1" in capsys.readouterr().out


def test_default_en_zh_engineering_glossary_is_selected() -> None:
    glossary = cli._glossary_for(Namespace(
        glossary=None,
        source_language="en",
        target_language="zh",
    ))

    assert glossary is not None
    assert [(entry.source, entry.target) for entry in glossary.entries] == [
        ("Clearing and Grubb", "清表及清根"),
        ("No.", "编号"),
    ]


def test_qwen_lite_is_rejected_without_output(tmp_path, monkeypatch, capsys):
    configure_fakes(monkeypatch)
    source = tmp_path / "source.md"
    destination = tmp_path / "translated.md"
    source.write_text("Hello", encoding="utf-8")

    assert cli.main([
        "translate-markdown", str(source), str(destination), "--source-language", "en", "--target-language", "zh",
        "--provider", "qwen-mt", "--model", "qwen-mt-lite",
    ]) == 1
    assert not destination.exists()
    assert "qwen-mt-plus or qwen-mt-flash" in capsys.readouterr().err

    # The default Qwen Chat provider refuses a Qwen-MT model name outright.
    assert cli.main([
        "translate-markdown", str(source), str(destination), "--source-language", "en", "--target-language", "zh",
        "--model", "qwen-mt-lite",
    ]) == 1
    assert not destination.exists()
    assert "Qwen Chat requires a non-Qwen-MT model" in capsys.readouterr().err


def test_required_languages_are_enforced(tmp_path):
    source = tmp_path / "source.md"
    destination = tmp_path / "translated.md"
    with pytest.raises(SystemExit) as caught:
        cli.main(["translate-markdown", str(source), str(destination), "--source-language", "en"])
    assert caught.value.code == 2


def test_markdown_and_pptx_accept_openai_provider() -> None:
    parser = cli._parser()

    markdown = parser.parse_args([
        "translate-markdown", "source.md", "translated.md",
        "--source-language", "en", "--target-language", "zh", "--provider", "openai",
    ])
    pptx = parser.parse_args([
        "translate-pptx", "source.pptx", "translated.pptx",
        "--source-language", "en", "--target-language", "zh", "--provider", "openai",
    ])

    assert markdown.provider == "openai"
    assert pptx.provider == "openai"


def test_xlsx_accepts_cloud_provider_and_hidden_sheet_option() -> None:
    args = cli._parser().parse_args([
        "translate-xlsx", "source.xlsx", "translated.xlsx",
        "--source-language", "en", "--target-language", "zh",
        "--provider", "openai", "--include-hidden-sheets",
    ])

    assert args.provider == "openai"
    assert args.include_hidden_sheets is True


def test_source_destination_match_is_rejected(tmp_path, monkeypatch, capsys):
    configure_fakes(monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("Hello", encoding="utf-8")

    assert cli.main(["translate-markdown", str(source), str(source), "--source-language", "en", "--target-language", "zh"]) == 1

    assert "paths must differ" in capsys.readouterr().err
    assert source.read_text(encoding="utf-8") == "Hello"


def test_cache_is_only_created_when_explicitly_requested(tmp_path, monkeypatch):
    configure_fakes(monkeypatch)
    source = tmp_path / "source.md"
    destination = tmp_path / "translated.md"
    cache_path = tmp_path / "chosen.sqlite3"
    source.write_text("Hello", encoding="utf-8")

    assert cli.main([
        "translate-markdown", str(source), str(destination), "--source-language", "en", "--target-language", "zh",
        "--cache", str(cache_path), "--translation-mode", "review", "--max-attempts", "2",
    ]) == 0

    service = FakeService.instances[0]
    assert len(FakeCache.instances) == 1
    assert service.cache.path == cache_path
    assert service.cache.closed
    assert service.translation_mode == "review"
    assert service.max_attempts == 2


def test_failure_returns_nonzero_without_destination(tmp_path, monkeypatch, capsys):
    configure_fakes(monkeypatch)
    FakeService.fail = True
    source = tmp_path / "source.md"
    destination = tmp_path / "translated.md"
    source.write_text("Hello", encoding="utf-8")

    assert cli.main(["translate-markdown", str(source), str(destination), "--source-language", "en", "--target-language", "zh"]) == 1

    assert not destination.exists()
    assert "provider unavailable" in capsys.readouterr().err
