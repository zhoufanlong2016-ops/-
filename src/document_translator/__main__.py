"""Command-line entry point for the stage-one Markdown translator."""

from __future__ import annotations

import argparse
import json
import sys
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

import httpx

from .providers import OpenAIConfig, OpenAIProvider, QwenChatConfig, QwenChatProvider, QwenMTConfig, QwenMTProvider
from .services import (
    DocxTranslationService,
    DwgTranslationService,
    PptxTranslationService,
    XlsxTranslationService,
    Glossary,
    MarkdownTranslationService,
    BabelDocPdfTranslationService,
    TranslationCache,
    load_glossary,
    write_docx_comparison_report,
)


_DEFAULT_ZH_EN_GLOSSARY = Path(__file__).with_name("assets") / "local_zh_en_glossary.csv"
_DEFAULT_EN_ZH_ENGINEERING_GLOSSARY = Path(__file__).with_name("assets") / "engineering_en_zh_glossary.csv"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="document-translator",
        description="Translate format-preserving documents using a configured cloud translation provider.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    translate = subparsers.add_parser("translate-markdown", help="translate a Markdown source file")
    translate.add_argument("source", type=Path, metavar="SOURCE")
    translate.add_argument("destination", type=Path, metavar="DESTINATION")
    translate.add_argument("--source-language", required=True)
    translate.add_argument("--target-language", required=True)
    translate.add_argument(
        "--provider",
        choices=("qwen-mt", "qwen", "openai"),
        default="qwen-mt",
        help="translation provider (default: qwen-mt)",
    )
    translate.add_argument(
        "--model",
        help="provider model (default: qwen-mt-plus)",
    )
    translate.add_argument("--cache", type=Path, metavar="PATH", help="explicit SQLite cache path")
    translate.add_argument("--glossary", type=Path, metavar="PATH", help="local CSV/XLSX terminology file")
    translate.add_argument("--translation-mode", default="default")
    translate.add_argument("--max-attempts", type=int, default=3, help="provider attempts per uncached unit")
    translate.add_argument("--overwrite", action="store_true", help="allow replacement of DESTINATION")
    docx = subparsers.add_parser("translate-docx", help="translate a DOCX source file paragraph by paragraph")
    docx.add_argument("source", type=Path, metavar="SOURCE")
    docx.add_argument("destination", type=Path, metavar="DESTINATION")
    docx.add_argument("--source-language", required=True)
    docx.add_argument("--target-language", required=True)
    docx.add_argument("--provider", choices=("qwen-mt", "qwen", "openai"), default="qwen-mt")
    docx.add_argument("--model")
    docx.add_argument("--max-attempts", type=int, default=3, help="provider attempts per paragraph")
    docx.add_argument("--comparison-report", type=Path, metavar="PATH", help="write source/translation/hash audit JSON")
    docx.add_argument("--glossary", type=Path, metavar="PATH", help="local CSV/XLSX terminology file")
    pptx = subparsers.add_parser("translate-pptx", help="translate PPTX text nodes")
    pptx.add_argument("source", type=Path)
    pptx.add_argument("destination", type=Path)
    pptx.add_argument("--source-language", required=True)
    pptx.add_argument("--target-language", required=True)
    pptx.add_argument("--provider", choices=("qwen-mt", "qwen", "openai"), default="qwen-mt")
    pptx.add_argument("--model", default="qwen-mt-plus")
    pptx.add_argument("--glossary", type=Path)
    pptx.add_argument("--layout-report", type=Path, metavar="PATH", help="write PowerPoint layout audit JSON")
    pptx.add_argument("--minimum-font-size", type=float, default=8.0, help="minimum readable font size in points")
    pptx.add_argument("--skip-layout-fit", action="store_true", help="skip PowerPoint-native layout fitting")
    xlsx = subparsers.add_parser("translate-xlsx", help="translate ordinary XLSX cell strings")
    xlsx.add_argument("source", type=Path, metavar="SOURCE")
    xlsx.add_argument("destination", type=Path, metavar="DESTINATION")
    xlsx.add_argument("--source-language", required=True)
    xlsx.add_argument("--target-language", required=True)
    xlsx.add_argument("--provider", choices=("qwen-mt", "qwen", "openai"), default="qwen-mt")
    xlsx.add_argument("--model")
    xlsx.add_argument("--glossary", type=Path, metavar="PATH", help="local CSV/XLSX terminology file")
    xlsx.add_argument("--max-attempts", type=int, default=3, help="provider attempts per cell")
    xlsx.add_argument("--include-hidden-sheets", action="store_true")
    pdf = subparsers.add_parser("translate-pdf", help="translate PDF through the BabelDOC layout-preserving worker")
    pdf.add_argument("source", type=Path); pdf.add_argument("destination", type=Path)
    pdf.add_argument("--source-language", required=True); pdf.add_argument("--target-language", required=True)
    pdf.add_argument("--provider", choices=("qwen", "gpt"), default="qwen")
    pdf.add_argument("--model", required=True, help="approved provider model for this PDF job")
    pdf.add_argument("--glossary", type=Path); pdf.add_argument("--style-profile", type=Path, metavar="PATH", help="JSON PDF structural/numbering style profile")
    pdf.add_argument("--minimum-font-size", type=float, default=6.0)
    pdf.add_argument("--gateway-base-url", help="local OpenAI-compatible Translation Gateway URL")
    pdf.add_argument("--report", type=Path, metavar="PATH", help="write the PDF preflight and candidate validation report")
    pdf.add_argument("--allow-cad-pdf", action="store_true", help="allow a class-C drawing-style PDF after confirming no source DWG is available")
    pdf.add_argument("--allow-complex-pdf", action="store_true", help="allow a class-B PDF after confirming that full visual review will be performed")
    dwg_import = subparsers.add_parser(
        "prepare-dwg-import",
        help="translate a CadBridge export JSON and create an AutoCAD import task",
    )
    dwg_import.add_argument("export_json", type=Path, metavar="EXPORT_JSON")
    dwg_import.add_argument("destination_dwg", type=Path, metavar="DESTINATION_DWG")
    dwg_import.add_argument("task_json", type=Path, metavar="TASK_JSON")
    dwg_import.add_argument("result_json", type=Path, metavar="RESULT_JSON")
    dwg_import.add_argument("command_script", type=Path, metavar="COMMAND_SCRIPT")
    dwg_import.add_argument("--source-language", required=True)
    dwg_import.add_argument("--target-language", required=True)
    dwg_import.add_argument("--provider", choices=("qwen-mt", "qwen", "openai"), default="qwen-mt")
    dwg_import.add_argument("--model")
    dwg_import.add_argument("--glossary", type=Path, metavar="PATH")
    dwg_import.add_argument("--max-attempts", type=int, default=3)
    dwg_import.add_argument("--overwrite", action="store_true")
    dwg_import.add_argument("--font-map", type=Path, metavar="PATH", help="JSON source-style to target-font map")
    validate = subparsers.add_parser("validate-output", help="validate a translated Markdown, Office, or XLSX output")
    validate.add_argument("output", type=Path, metavar="OUTPUT")
    validate.add_argument("--type", choices=("auto", "markdown", "docx", "pptx", "xlsx"), default="auto")
    validate.add_argument("--target-language", default="en", help="expected output language for script checks")
    subparsers.add_parser("gui", help="open the drag-and-drop desktop translation interface")
    return parser


def _glossary_for(args: argparse.Namespace) -> Glossary | None:
    if args.glossary is not None:
        return load_glossary(args.glossary)
    source = args.source_language.casefold()
    target = args.target_language.casefold()
    if source in {"zh", "zh-cn"} and target in {"en", "en-us", "en-gb"}:
        return load_glossary(_DEFAULT_ZH_EN_GLOSSARY)
    if source in {"en", "en-us", "en-gb", "english"} and target in {"zh", "zh-cn", "chinese"}:
        return load_glossary(_DEFAULT_EN_ZH_ENGINEERING_GLOSSARY)
    return None


def _provider_for(args: argparse.Namespace, client: httpx.Client, glossary: Glossary | None = None):
    if args.provider == "openai":
        if args.model is None:
            raise ValueError("--model is required with provider openai")
        return OpenAIProvider(OpenAIConfig(model=args.model), client=client, glossary=glossary)
    if args.provider == "qwen-mt":
        config = QwenMTConfig(model=args.model if args.model is not None else "qwen-mt-plus")
        return QwenMTProvider(config, client=client, glossary=glossary)
    if args.provider == "qwen":
        if args.model is None:
            raise ValueError("--model is required with provider qwen")
        return QwenChatProvider(QwenChatConfig(model=args.model), client=client, glossary=glossary)



def _translate_markdown(args: argparse.Namespace) -> int:
    if args.source.resolve() == args.destination.resolve():
        raise ValueError("source and destination paths must differ")

    cache: TranslationCache | None = None
    try:
        with httpx.Client() as client:
            provider = _provider_for(args, client, _glossary_for(args))
            if args.cache is not None:
                cache = TranslationCache(args.cache)
            service = MarkdownTranslationService(
            provider,
            cache,
            translation_mode=args.translation_mode,
            max_attempts=args.max_attempts,
            # Keep individual provider requests short enough that immutable
            # numbers, units and codes are not silently dropped by Qwen-MT.
            max_segment_chars=480,
        )
            outcome = service.translate_file(
                args.source,
                args.destination,
                source_language=args.source_language,
                target_language=args.target_language,
                overwrite=args.overwrite,
            )
    finally:
        if cache is not None:
            cache.close()

    print(
        f"translated {len(outcome.units)} units; cache hits={outcome.cache_hits}; "
        f"cache misses={outcome.cache_misses}; output={args.destination}"
    )
    for warning in outcome.preflight_warnings:
        print(f"preflight warning: {warning}", file=sys.stderr)
    return 0


def _translate_docx(args: argparse.Namespace) -> int:
    if args.source.resolve() == args.destination.resolve():
        raise ValueError("source and destination paths must differ")
    with httpx.Client() as client:
        outcome = DocxTranslationService(
            _provider_for(args, client, _glossary_for(args)), max_attempts=args.max_attempts,
        ).translate_file(
            args.source, args.destination,
            source_language=args.source_language, target_language=args.target_language,
        )
    print(f"translated {len(outcome.units)} paragraphs; output={args.destination}")
    if args.comparison_report is not None:
        write_docx_comparison_report(args.comparison_report, outcome)
        print(f"comparison report={args.comparison_report}")
    for issue in outcome.quality_issues:
        print(f"quality issue: {issue}; draft retained for review", file=sys.stderr)
    return 2 if outcome.quality_issues else 0


def _validate_output(args: argparse.Namespace) -> int:
    path = args.output
    kind = args.type
    if kind == "auto":
        kind = {".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx"}.get(path.suffix.casefold(), "markdown")
    if not path.is_file():
        raise FileNotFoundError(path)
    if kind in {"docx", "pptx"}:
        with zipfile.ZipFile(path) as package:
            if kind == "docx":
                root = ET.fromstring(package.read("word/document.xml"))
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                text = "".join(node.text or "" for node in root.findall(".//w:t", ns))
            else:
                text = ""
                for name in package.namelist():
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name):
                        root = ET.fromstring(package.read(name))
                        text += "".join(node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"))
    elif kind == "xlsx":
        from .adapters.xlsx import read_xlsx

        text = "\n".join(unit.source_text for unit in read_xlsx(path).units)
    else:
        text = path.read_text(encoding="utf-8-sig")
    issues: list[str] = []
    if args.target_language.casefold() in {"en", "en-us", "en-gb", "english"} and re.search(r"[\u3400-\u9fff]", text):
        issues.append("CJK_RESIDUE")
    if "\u2011" in text:
        issues.append("NONBREAKING_HYPHEN")
    if "\u2014" in text:
        issues.append("EM_DASH")
    if any(marker in text for marker in ("JSON output", "unit_id", "Return a complete English translation")):
        issues.append("PROMPT_ECHO")
    if issues:
        print(f"validation failed: {', '.join(issues)}", file=sys.stderr)
        return 2
    print(f"validation passed: {path}")
    return 0


def _translate_pptx(args: argparse.Namespace) -> int:
    if args.source.resolve() == args.destination.resolve():
        raise ValueError("source and destination paths must differ")
    with httpx.Client() as client:
        count = PptxTranslationService(
            _provider_for(args, client, _glossary_for(args)),
        ).translate_file(args.source, args.destination, source_language=args.source_language, target_language=args.target_language)
    if args.skip_layout_fit:
        print(f"translated {count} PPTX text paragraphs; output={args.destination}; layout fitting skipped")
        return 0
    from .services import PptxLayoutService
    report = args.layout_report or args.destination.with_suffix(".layout.json")
    outcome = PptxLayoutService(minimum_font_size=args.minimum_font_size).fit_file(args.source, args.destination, report)
    print(f"translated {count} PPTX text paragraphs; output={args.destination}; layout_report={outcome.report_path}; unresolved={outcome.unresolved}")
    return 2 if outcome.unresolved else 0


def _translate_xlsx(args: argparse.Namespace) -> int:
    if args.source.resolve() == args.destination.resolve():
        raise ValueError("source and destination paths must differ")
    with httpx.Client() as client:
        outcome = XlsxTranslationService(
            _provider_for(args, client, _glossary_for(args)), max_attempts=args.max_attempts,
        ).translate_file(
            args.source,
            args.destination,
            source_language=args.source_language,
            target_language=args.target_language,
            include_hidden_sheets=args.include_hidden_sheets,
        )
    print(
        f"translated {len(outcome.units)} XLSX text cells; "
        f"skipped={len(outcome.skipped)}; output={args.destination}",
    )
    for item in outcome.skipped:
        print(f"skipped: {item}", file=sys.stderr)
    return 0

def _translate_pdf(args: argparse.Namespace) -> int:
    if args.source.resolve() == args.destination.resolve(): raise ValueError("source and destination paths must differ")
    if args.glossary is not None:
        glossary = args.glossary
    elif args.source_language.casefold() in {"zh", "zh-cn", "chinese"}:
        glossary = _DEFAULT_ZH_EN_GLOSSARY
    else:
        glossary = _DEFAULT_EN_ZH_ENGINEERING_GLOSSARY
    output, preflight, report = BabelDocPdfTranslationService().translate_file(
        args.source, args.destination, source_language=args.source_language,
        target_language=args.target_language, provider=args.provider,
        model=args.model, glossary=glossary if glossary.exists() else None,
        style_profile=args.style_profile,
        report_path=args.report, allow_cad_pdf=args.allow_cad_pdf,
        allow_complex_pdf=args.allow_complex_pdf,
        minimum_font_size=args.minimum_font_size,
        gateway_base_url=args.gateway_base_url,
    )
    print(f"translated PDF with BabelDOC; class={preflight.classification}; output={output}; report={report}")
    return 0


def _prepare_dwg_import(args: argparse.Namespace) -> int:
    """Create a reviewable TR_IMPORT task; AutoCAD remains a separate final step."""
    font_policy = None
    if args.font_map is not None:
        payload = json.loads(args.font_map.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("font map must be a JSON object")
        font_policy = {}
        for key, value in payload.items():
            if isinstance(value, str):
                font_policy[str(key)] = value
            elif isinstance(value, dict) and isinstance(value.get("zh_regular"), str):
                font_policy[str(key)] = value["zh_regular"]
            elif isinstance(value, dict) and any(
                field in value for field in ("target_font_file", "target_style_name", "width_factor")
            ):
                font_policy[str(key)] = value
            else:
                raise ValueError(
                    f"font map entry {key!r} must be a font name, contain zh_regular, "
                    "or contain target_font_file/target_style_name",
                )
    with httpx.Client() as client:
        outcome = DwgTranslationService(
            _provider_for(args, client, _glossary_for(args)), max_attempts=args.max_attempts,
        ).prepare_import(
            args.export_json,
            destination_dwg=args.destination_dwg,
            task_json=args.task_json,
            result_json=args.result_json,
            command_script=args.command_script,
            source_language=args.source_language,
            target_language=args.target_language,
            overwrite=args.overwrite,
            font_policy=font_policy,
        )
    print(
        f"translated {len(outcome.units)} DWG text items; "
        f"import task={outcome.task_json}; script={outcome.command_script}",
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # Keep CLI status/error paths readable when the GUI launches this process
    # on Windows, whose inherited console encoding may be a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "gui":
            from .gui import run_gui

            return run_gui()
        if args.command == "translate-markdown":
            return _translate_markdown(args)
        if args.command == "translate-docx":
            return _translate_docx(args)
        if args.command == "translate-pptx":
            return _translate_pptx(args)
        if args.command == "translate-xlsx":
            return _translate_xlsx(args)
        if args.command == "translate-pdf":
            return _translate_pdf(args)
        if args.command == "prepare-dwg-import":
            return _prepare_dwg_import(args)
        return _validate_output(args)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
