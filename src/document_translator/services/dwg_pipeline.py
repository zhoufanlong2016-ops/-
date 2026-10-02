"""One-step DWG translation: CadBridge export -> translation -> CadBridge import.

AutoCAD's own headless console (accoreconsole.exe) runs the two CadBridge
commands, so a dropped DWG goes through the same validated path that was
previously run by hand inside AutoCAD. The source DWG is only read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from document_translator.adapters import dwg
from document_translator.language_detect import detect_text_language, other_language

from .dwg_translation import DwgTranslationService


class DwgPipelineError(RuntimeError):
    """The DWG could not be exported, translated or written back."""


class DwgLanguageUndetermined(DwgPipelineError):
    """The drawing's language could not be decided; the caller must choose."""


_CJK_FONT_KEYS = ("simhei", "simsun", "simfang", "simkai", "msyh", "fangsong", "kaiti", "nsimsun", "dengxian", "gbcbig", "hztxt", "hzfs")
_TIMEOUT_SECONDS = 900


@dataclass(frozen=True, slots=True)
class DwgPipelineOutcome:
    destination: Path
    item_count: int
    source_language: str
    target_language: str


def find_accoreconsole() -> Path:
    override = os.environ.get("DOCUMENT_TRANSLATOR_ACCORECONSOLE")
    candidates: list[Path] = [Path(override)] if override else []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Autodesk\AutoCAD") as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                release = winreg.EnumKey(root, index)
                # CadBridge is built against AutoCAD 2025 (R25, .NET 8).
                if not release.startswith("R25"):
                    continue
                with winreg.OpenKey(root, release) as release_key:
                    for product_index in range(winreg.QueryInfoKey(release_key)[0]):
                        product = winreg.EnumKey(release_key, product_index)
                        try:
                            with winreg.OpenKey(release_key, product) as product_key:
                                location = winreg.QueryValueEx(product_key, "AcadLocation")[0]
                        except OSError:
                            continue
                        if location:
                            candidates.append(Path(location) / "accoreconsole.exe")
    except OSError:
        pass
    candidates += [
        Path(drive) / "Program Files" / "Autodesk" / "AutoCAD 2025" / "accoreconsole.exe" for drive in ("C:\\", "D:\\")
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DwgPipelineError("未找到 AutoCAD 2025 的 accoreconsole.exe；DWG 翻译需要本机安装 AutoCAD 2025")


def find_cadbridge() -> Path:
    override = os.environ.get("DOCUMENT_TRANSLATOR_CADBRIDGE")
    roots = [Path(getattr(sys, "_MEIPASS", ""))] if getattr(sys, "frozen", False) else []
    roots.append(Path(__file__).resolve().parents[3])
    candidates = [Path(override)] if override else []
    candidates += [root / "cad" / "CadBridge.bundle" / "Contents" / "CadBridge.dll" for root in roots]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DwgPipelineError("未找到 CadBridge.dll（cad\\CadBridge.bundle\\Contents）")


def _run_script(console: Path, script: Path, workdir: Path, label: str) -> str:
    """Run one AutoCAD script headlessly; return its console log."""
    try:
        completed = subprocess.run(
            [str(console), "/s", str(script), "/l", "zh-CN"],
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise DwgPipelineError(f"AutoCAD {label} 超时（{_TIMEOUT_SECONDS} 秒）") from error
    # accoreconsole writes UTF-16LE.
    log = completed.stdout.decode("utf-16-le", errors="replace").replace("\x00", "").replace("\r", "")
    (workdir / f"{label}.log").write_text(log, encoding="utf-8")
    if "failed:" in log:
        tail = log[log.rfind("failed:") :][:600]
        raise DwgPipelineError(f"CadBridge {label} 失败：{tail}")
    return log


def _font_policy_for_chinese(exported: dwg.ExportDocument) -> dict[str, dict[str, object]]:
    """Styles whose font cannot show Chinese get SimHei, as the earlier
    manual runs did with font-map.json; Chinese-capable styles are left as is."""
    policy: dict[str, dict[str, object]] = {}
    for item in exported.items:
        meta = item.metadata
        fonts = f"{meta.font_file or ''} {meta.big_font_file or ''}".casefold()
        if any(key in fonts for key in _CJK_FONT_KEYS) or (meta.is_shape_file and meta.big_font_file):
            continue
        policy[meta.style_handle] = {"target_font_file": "simhei.ttf", "apply_to_mtext": True}
    return policy


def translate_dwg_file(
    provider_factory: Callable[[str, str], object],
    source: str | Path,
    destination: str | Path,
    *,
    source_language: str,
    target_language: str,
    progress: Callable[[str], None] = print,
) -> DwgPipelineOutcome:
    source_path, destination_path = Path(source).resolve(), Path(destination).resolve()
    if source_path == destination_path:
        raise DwgPipelineError("输出路径不能与源 DWG 相同")
    if destination_path.exists():
        raise FileExistsError("destination already exists; choose a new path")
    console, bridge = find_accoreconsole(), find_cadbridge()
    workdir = Path(tempfile.mkdtemp(prefix="document-translator-dwg-", dir=destination_path.parent))
    try:
        export_json = workdir / "export.json"
        dwg.write_export_task(workdir / "export_task.json", source_dwg=source_path, export_json=export_json)
        dwg.write_command_script(
            workdir / "export.scr", operation="export", task_json=workdir / "export_task.json", netload_path=bridge,
        )
        progress("dwg: AutoCAD 导出图中文字")
        _run_script(console, workdir / "export.scr", workdir, "export")
        if not export_json.is_file():
            raise DwgPipelineError("CadBridge 导出没有生成文字清单，详见 export.log")
        exported = dwg.read_export_document(export_json)
        progress(f"dwg: 导出 {len(exported.items)} 条文字")

        if source_language == "auto":
            detected = detect_text_language("\n".join(item.source_text for item in exported.items))
            if detected is None:
                raise DwgLanguageUndetermined("无法判断图纸语言，请手动选择源语言")
            source_language = detected
        if target_language == "auto":
            target_language = other_language(source_language)
        progress(f"dwg: 语言 {source_language} -> {target_language}")

        font_policy = _font_policy_for_chinese(exported) if target_language.startswith("zh") else None
        DwgTranslationService(provider_factory(source_language, target_language)).prepare_import(
            export_json,
            destination_dwg=workdir / "translated.dwg",
            task_json=workdir / "import_task.json",
            result_json=workdir / "import_result.json",
            command_script=workdir / "import_plain.scr",
            source_language=source_language,
            target_language=target_language,
            font_policy=font_policy,
        )
        dwg.write_command_script(
            workdir / "import.scr", operation="import", task_json=workdir / "import_task.json", netload_path=bridge,
        )
        progress("dwg: AutoCAD 写回译文")
        _run_script(console, workdir / "import.scr", workdir, "import")
        translated = workdir / "translated.dwg"
        if not translated.is_file() or not (workdir / "import_result.json").is_file():
            raise DwgPipelineError("CadBridge 导入没有生成译文图纸，详见 import.log")
        shutil.move(str(translated), str(destination_path))
    except Exception:
        progress(f"dwg: 失败，中间文件保留在 {workdir}")
        raise
    shutil.rmtree(workdir, ignore_errors=True)
    return DwgPipelineOutcome(destination_path, len(exported.items), source_language, target_language)
