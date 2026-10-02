from pathlib import Path

import pytest

from document_translator.gui import PROVIDER_MODELS, build_cli_command, default_destination, detect_format


def test_gui_exposes_only_the_three_recommended_qwen_models() -> None:
    assert PROVIDER_MODELS["qwen"] == ("qwen3.7-plus", "qwen3.8-flash", "qwen3.8-max")
    assert "qwen-mt" not in PROVIDER_MODELS


def test_detect_format_and_destination(tmp_path: Path) -> None:
    source = tmp_path / "合同.docx"
    source.write_text("placeholder", encoding="utf-8")
    assert detect_format(source) == "Word DOCX"
    destination = default_destination(source)
    assert destination.name == "合同_translated.docx"
    destination.write_text("existing", encoding="utf-8")
    assert default_destination(source).name == "合同_translated_2.docx"


def test_build_markdown_command_includes_optional_glossary(tmp_path: Path) -> None:
    command = build_cli_command(
        tmp_path / "source.md",
        tmp_path / "target.md",
        provider="qwen-mt",
        model="qwen-mt-plus",
        source_language="auto",
        target_language="en",
        glossary=tmp_path / "terms.csv",
    )
    assert command[3:6] == ["translate-markdown", str(tmp_path / "source.md"), str(tmp_path / "target.md")]
    assert "--glossary" in command


def test_pdf_rejects_qwen_mt_endpoint(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="qwen-mt"):
        build_cli_command(
            tmp_path / "source.pdf",
            tmp_path / "target.pdf",
            provider="qwen-mt",
            model="qwen-mt-plus",
            source_language="en",
            target_language="zh",
        )


def test_dwg_runs_the_one_step_autocad_pipeline(tmp_path: Path) -> None:
    command = build_cli_command(
        tmp_path / "source.dwg",
        tmp_path / "target.dwg",
        provider="qwen",
        model="qwen3.8-flash",
        source_language="auto",
        target_language="auto",
    )
    assert command[3:6] == ["translate-dwg", str(tmp_path / "source.dwg"), str(tmp_path / "target.dwg")]
    assert command[command.index("--source-language") + 1] == "auto"


def test_input_holds_one_file_and_newest_supported_file_wins(tmp_path: Path) -> None:
    import tkinter as tk

    from document_translator.gui import TranslationApp

    first, second, other = tmp_path / "a.xlsx", tmp_path / "b.pdf", tmp_path / "c.txt"
    for path in (first, second, other):
        path.write_bytes(b"x")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        root.withdraw()
        app = TranslationApp(root)
        app.add_files([str(first)])
        assert app.files == [first]
        app.add_files([str(other), str(second), str(first)])
        assert app.files == [second]
        app.add_files([str(other)])
        assert app.files == [second]
    finally:
        root.destroy()
