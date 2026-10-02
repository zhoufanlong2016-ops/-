"""Small desktop front-end for the document translation pipeline.

The GUI deliberately delegates translation to the existing CLI.  This keeps
provider routing, caching, validation and atomic output behavior in one place
instead of creating a second translation implementation.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable


SUPPORTED_FORMATS = {
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".docx": "Word DOCX",
    ".pptx": "PowerPoint PPTX",
    ".xlsx": "Excel XLSX",
    ".pdf": "PDF",
    ".dwg": "AutoCAD DWG",
}

PROVIDER_MODELS = {
    "qwen": ("qwen3.7-plus", "qwen3.8-flash", "qwen3.8-max"),
    "openai": ("gpt-5.6-luna", "gpt-5.6-terra"),
    "deepseek": ("deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-v4-pro"),
}
LANGUAGE_CODES = {"自动": "auto", "中文": "zh", "英文": "en"}
LANGUAGE_NAMES = {"zh": "中文", "en": "英文"}
# translate-dwg exit code: the drawing's language could not be decided.
LANGUAGE_UNDETERMINED_EXIT = 3


def detect_format(path: str | Path) -> str:
    """Return the user-facing format name for a supported file."""
    suffix = Path(path).suffix.casefold()
    try:
        return SUPPORTED_FORMATS[suffix]
    except KeyError as error:
        raise ValueError(f"不支持的文件格式: {suffix or '(无扩展名)'}") from error


def default_destination(source: str | Path, output_dir: str | Path | None = None) -> Path:
    """Build a non-overwriting destination name next to or below the source."""
    source_path = Path(source)
    directory = Path(output_dir) if output_dir else source_path.parent
    candidate = directory / f"{source_path.stem}_translated{source_path.suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{source_path.stem}_translated_{index}{source_path.suffix}"
        index += 1
    return candidate


def build_cli_command(
    source: str | Path,
    destination: str | Path,
    *,
    provider: str,
    model: str,
    source_language: str,
    target_language: str,
    glossary: str | Path | None = None,
    pdf_cache: str | Path | None = None,
) -> list[str]:
    """Build the existing CLI command for one supported input file."""
    source_path = Path(source)
    suffix = source_path.suffix.casefold()
    commands = {".md": "translate-markdown", ".markdown": "translate-markdown", ".docx": "translate-docx", ".pptx": "translate-pptx", ".xlsx": "translate-xlsx", ".pdf": "translate-pdf", ".dwg": "translate-dwg"}
    command = commands[suffix]
    if suffix == ".pdf" and provider == "qwen-mt":
        raise ValueError("PDF 需要选择 qwen（Chat）、openai 或 deepseek，不能使用 qwen-mt 翻译端点")
    if suffix == ".pdf":
        actual_provider = {"openai": "gpt", "deepseek": "deepseek"}.get(provider, "qwen")
    else:
        actual_provider = provider
    executable_args = [command] if getattr(sys, "frozen", False) else ["-m", "document_translator", command]
    args = [sys.executable, *executable_args, str(source_path), str(destination), "--source-language", source_language, "--target-language", target_language, "--provider", actual_provider, "--model", model]
    if glossary:
        args.extend(("--glossary", str(glossary)))
    if suffix == ".pdf":
        # The in-place engine keeps every page object, so drawing PDFs (class
        # C) are safe to translate from the GUI as well.
        args.extend((
            "--engine", "inplace", "--allow-complex-pdf", "--allow-cad-pdf",
            "--report", str(Path(destination).with_suffix(".report.json")),
        ))
        if pdf_cache is not None:
            args.extend(("--cache", str(pdf_cache)))
    elif suffix == ".docx":
        args.extend(("--comparison-report", str(Path(destination).with_suffix(".comparison.json"))))
    elif suffix == ".pptx":
        args.extend(("--layout-report", str(Path(destination).with_suffix(".layout.json"))))
    return args


class _WindowsDropTarget:
    """Receive native Windows Explorer file drops without extra packages."""

    def __init__(self, root: tk.Tk, callback: Callable[[list[str]], None]) -> None:
        self.root = root
        self.callback = callback
        self._old_proc = None
        self._proc = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            hwnd = root.winfo_id()
            user32.DragAcceptFiles(hwnd, True)
            self._user32 = user32
            self._shell32 = shell32
            self._hwnd = hwnd
            self._call_window_proc = user32.CallWindowProcW
            self._call_window_proc.restype = ctypes.c_ssize_t
            self._old_proc = user32.GetWindowLongPtrW(hwnd, -4)
            wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

            def wndproc(window, message, wparam, lparam):
                if message == 0x0233:  # WM_DROPFILES
                    count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                    paths = []
                    for index in range(count):
                        length = shell32.DragQueryFileW(wparam, index, None, 0)
                        buffer = ctypes.create_unicode_buffer(length + 1)
                        shell32.DragQueryFileW(wparam, index, buffer, length + 1)
                        paths.append(buffer.value)
                    shell32.DragFinish(wparam)
                    root.after(0, lambda: callback(paths))
                    return 0
                return self._call_window_proc(self._old_proc, window, message, wparam, lparam)

            self._proc = wndproc_type(wndproc)
            user32.SetWindowLongPtrW(hwnd, -4, self._proc)
            root.bind("<Destroy>", self.close, add="+")
        except Exception:
            self._old_proc = None

    def close(self, _event=None) -> None:
        if self._old_proc and getattr(self, "_user32", None):
            self._user32.SetWindowLongPtrW(self._hwnd, -4, self._old_proc)
            self._old_proc = None


class TranslationApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("文档保真翻译器")
        self.root.geometry("900x650")
        self.root.minsize(760, 560)
        self.files: list[Path] = []
        self.provider = tk.StringVar(value="qwen")
        self.model = tk.StringVar(value="qwen3.8-flash")
        self.source_language = tk.StringVar(value="自动")
        self.target_language = tk.StringVar(value="自动")
        self.glossary = tk.StringVar()
        self.status = tk.StringVar(value="请拖入文件，或点击“选择文件”")
        self.elapsed = tk.StringVar(value="运行时间：00:00:00")
        self._run_started_at: float | None = None
        self._elapsed_timer: str | None = None
        self._stop_requested = threading.Event()
        self._active_process: subprocess.Popen[bytes] | None = None
        self._active_process_lock = threading.Lock()
        self._build()
        self._drop_target = _WindowsDropTarget(root, self.add_files)

    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="文档保真翻译器", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="自动识别格式 · 批量调用模型 · 保留原文件并导出新文件", foreground="#5b6472").pack(anchor="w", pady=(0, 12))
        drop = ttk.LabelFrame(outer, text="输入文件（支持拖放）", padding=10)
        drop.pack(fill="both", expand=True)
        self.file_list = tk.Listbox(drop, height=8, activestyle="none", selectmode="extended")
        self.file_list.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(drop, orient="vertical", command=self.file_list.yview)
        scrollbar.pack(side="right", fill="y")
        self.file_list.configure(yscrollcommand=scrollbar.set)
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="选择文件", command=self.choose_files).pack(side="left")
        ttk.Button(buttons, text="清空", command=self.clear_files).pack(side="left", padx=6)
        ttk.Label(buttons, textvariable=self.status).pack(side="right")
        settings = ttk.LabelFrame(outer, text="翻译设置", padding=10)
        settings.pack(fill="x")
        for column in range(4):
            settings.columnconfigure(column, weight=1)
        ttk.Label(settings, text="服务商").grid(row=0, column=0, sticky="w")
        provider_box = ttk.Combobox(settings, textvariable=self.provider, values=tuple(PROVIDER_MODELS), state="readonly")
        provider_box.grid(row=1, column=0, sticky="ew", padx=(0, 8)); provider_box.bind("<<ComboboxSelected>>", self._provider_changed)
        ttk.Label(settings, text="模型").grid(row=0, column=1, sticky="w")
        self.model_box = ttk.Combobox(settings, textvariable=self.model, state="readonly")
        self.model_box.grid(row=1, column=1, sticky="ew", padx=(0, 8)); self._provider_changed()
        ttk.Label(settings, text="源语言").grid(row=0, column=2, sticky="w")
        ttk.Combobox(settings, textvariable=self.source_language, values=tuple(LANGUAGE_CODES), state="readonly").grid(row=1, column=2, sticky="ew", padx=(0, 8))
        ttk.Label(settings, text="目标语言").grid(row=0, column=3, sticky="w")
        ttk.Combobox(settings, textvariable=self.target_language, values=tuple(LANGUAGE_CODES), state="readonly").grid(row=1, column=3, sticky="ew")
        ttk.Label(settings, text="CSV/XLSX 术语库（可选）").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Entry(settings, textvariable=self.glossary).grid(row=3, column=0, columnspan=3, sticky="ew", padx=(0, 8))
        ttk.Button(settings, text="选择术语库", command=self.choose_glossary).grid(row=3, column=3, sticky="ew")
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 8))
        self.execute_button = ttk.Button(actions, text="执行翻译并导出", command=self.execute)
        self.execute_button.pack(side="left", fill="x", expand=True, ipady=6)
        self.stop_button = ttk.Button(actions, text="停止当前任务", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0), ipadx=12, ipady=6)
        self.progress = ttk.Progressbar(outer, orient="horizontal", mode="determinate", maximum=1, value=0)
        self.progress.pack(fill="x", pady=(0, 8))
        ttk.Label(outer, textvariable=self.elapsed, foreground="#5b6472").pack(anchor="e", pady=(0, 6))
        self.log = tk.Text(outer, height=8, state="disabled", wrap="word", background="#f7f8fa")
        self.log.pack(fill="both", expand=False)

    def _provider_changed(self, _event=None) -> None:
        values = PROVIDER_MODELS[self.provider.get()]
        self.model_box.configure(values=values)
        if self.model.get() not in values:
            self.model.set(values[0])

    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(filetypes=[("支持的文档", "*.md *.markdown *.docx *.pptx *.xlsx *.pdf *.dwg"), ("所有文件", "*.*")])
        self.add_files(list(paths))

    def add_files(self, paths: Iterable[str]) -> None:
        for raw in paths:
            path = Path(raw)
            if not path.is_file() or path.suffix.casefold() not in SUPPORTED_FORMATS:
                continue
            if path not in self.files:
                self.files.append(path); self.file_list.insert("end", f"{SUPPORTED_FORMATS[path.suffix.casefold()]}  ·  {path}")
        self.status.set(f"已选择 {len(self.files)} 个文件")

    def clear_files(self) -> None:
        self.files.clear(); self.file_list.delete(0, "end"); self.status.set("请拖入文件，或点击“选择文件”")

    def choose_glossary(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("术语库", "*.csv *.xlsx"), ("CSV", "*.csv"), ("Excel", "*.xlsx")])
        if path: self.glossary.set(path)

    def _write_log(self, text: str) -> None:
        self.log.configure(state="normal"); self.log.insert("end", text); self.log.see("end"); self.log.configure(state="disabled")

    def execute(self) -> None:
        if not self.files:
            messagebox.showwarning("缺少输入", "请先拖入或选择至少一个文件")
            return
        self.execute_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._stop_requested.clear()
        self.progress.configure(mode="determinate", maximum=len(self.files), value=0)
        self._run_started_at = time.monotonic()
        self._refresh_elapsed()
        threading.Thread(target=self._run_jobs, daemon=True).start()

    def stop(self) -> None:
        """Stop only the CLI/worker process started by this window."""
        self._stop_requested.set()
        with self._active_process_lock:
            process = self._active_process
        if process is None or process.poll() is not None:
            self._write_log("已请求停止；当前没有可终止的翻译进程。\n")
            return
        self._write_log("正在停止当前翻译任务……\n")
        try:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    process.kill()
        except OSError as error:
            self._write_log(f"停止翻译进程失败：{error}\n")

    def _run_jobs(self) -> None:
        files = list(self.files)
        for index, source in enumerate(files, start=1):
            if self._stop_requested.is_set():
                break
            temporary_path: Path | None = None
            keep_cache_file = False
            file_started_at = time.monotonic()
            try:
                project_root = (
                    Path(sys.executable).resolve().parent
                    if getattr(sys, "frozen", False)
                    else Path(__file__).resolve().parents[2]
                )
                cache_dir = project_root / ".cache" / "translated"
                cache_dir.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(prefix=f"{source.stem}_translated_", suffix=source.suffix, dir=cache_dir, delete=False)
                temporary_path = Path(handle.name)
                handle.close()
                temporary_path.unlink()
                languages = self._resolve_languages(source)
                if languages is None:
                    self.root.after(0, self._write_log, f"跳过：{source.name}（未确定语言）\n")
                    continue
                source_language, target_language = languages
                self.root.after(0, self._write_log, f"开始：{source.name}（完成后预览并选择保存位置）\n")
                self.root.after(0, self._start_file_progress)
                environment = os.environ.copy()
                source_root = str(project_root / "src")
                environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
                while True:
                    command = build_cli_command(source, temporary_path, provider=self.provider.get(), model=self.model.get(), source_language=source_language, target_language=target_language, glossary=self.glossary.get().strip() or None, pdf_cache=cache_dir.parent / "pdf_translation_cache.sqlite3")
                    code = self._run_command(command, project_root, environment)
                    # A DWG's language is only known after AutoCAD exports its text.
                    if code != LANGUAGE_UNDETERMINED_EXIT or source.suffix.casefold() != ".dwg" or self._stop_requested.is_set():
                        break
                    answer = self._ask_language(source)
                    if answer is None:
                        break
                    source_language, target_language = answer
                self.root.after(0, self._stop_file_progress)
                file_elapsed = time.monotonic() - file_started_at
                self.root.after(0, self._write_log, f"完成：{source.name}，退出码 {code}，耗时 {self._format_elapsed(file_elapsed)}\n")
                if self._stop_requested.is_set():
                    self.root.after(0, self._write_log, "已停止：临时文件将被清理。\n")
                elif code == 0 and temporary_path.exists():
                    keep_cache_file = True
                    self._request_save(temporary_path, source)
                elif temporary_path.exists():
                    self.root.after(0, self._write_log, "未保存：翻译未通过，临时文件已保留供排查。\n")
            except Exception as error:
                self.root.after(0, self._write_log, f"失败：{source.name}：{error}\n")
            finally:
                with self._active_process_lock:
                    self._active_process = None
                self.root.after(0, self._stop_file_progress)
                if temporary_path is not None and temporary_path.exists() and not keep_cache_file:
                    try:
                        temporary_path.unlink()
                    except OSError:
                        pass
                self.root.after(0, lambda completed=index: self.progress.configure(mode="determinate", value=completed))
        self.root.after(0, self._finish_elapsed)

    def _run_command(self, command: list[str], project_root: Path, environment: dict[str, str]) -> int:
        process = subprocess.Popen(command, cwd=str(project_root), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False)
        with self._active_process_lock:
            self._active_process = process
        assert process.stdout is not None
        for line in process.stdout:
            self.root.after(0, self._write_log, line.decode("utf-8", errors="replace"))
        code = process.wait()
        with self._active_process_lock:
            self._active_process = None
        return code

    def _resolve_languages(self, source: Path) -> tuple[str, str] | None:
        """Explicit choices win; "自动" is decided from the file, and the user is
        asked only when that is not possible. None means skip the file."""
        from document_translator.language_detect import detect_document_language, other_language

        source_language = LANGUAGE_CODES[self.source_language.get()]
        target_language = LANGUAGE_CODES[self.target_language.get()]
        if source_language == "auto" and source.suffix.casefold() == ".dwg":
            # Decided by translate-dwg from the exported text.
            return source_language, target_language
        if source_language == "auto":
            try:
                detected = detect_document_language(source)
            except Exception:
                detected = None
            if detected is None or detected == target_language:
                return self._ask_language(source)
            source_language = detected
            self.root.after(0, self._write_log, f"自动判断：{source.name} 是{LANGUAGE_NAMES[detected]}\n")
        if target_language == "auto" or target_language == source_language:
            target_language = other_language(source_language)
        return source_language, target_language

    def _ask_language(self, source: Path) -> tuple[str, str] | None:
        """Ask on the UI thread from the worker thread; wait for the answer."""
        answer: dict[str, bool | None] = {}
        done = threading.Event()

        def ask() -> None:
            answer["zh"] = messagebox.askyesnocancel(
                "请确认语言",
                f"无法自动判断“{source.name}”的语言。\n\n"
                "是：这是中文文件，译为英文\n否：这是英文文件，译为中文\n取消：跳过这个文件",
                parent=self.root,
            )
            done.set()

        self.root.after(0, ask)
        done.wait()
        if answer.get("zh") is None:
            return None
        return ("zh", "en") if answer["zh"] else ("en", "zh")

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, round(seconds))
        return f"{total // 3600:02}:{(total % 3600) // 60:02}:{total % 60:02}"

    def _refresh_elapsed(self) -> None:
        if self._run_started_at is None:
            return
        self.elapsed.set(f"运行时间：{self._format_elapsed(time.monotonic() - self._run_started_at)}")
        self._elapsed_timer = self.root.after(1000, self._refresh_elapsed)

    def _finish_elapsed(self) -> None:
        if self._elapsed_timer is not None:
            self.root.after_cancel(self._elapsed_timer)
            self._elapsed_timer = None
        if self._run_started_at is not None:
            self.elapsed.set(f"总耗时：{self._format_elapsed(time.monotonic() - self._run_started_at)}")
        self._run_started_at = None
        self.execute_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _start_file_progress(self) -> None:
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

    def _stop_file_progress(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")

    def _request_save(self, temporary_path: Path, source: Path) -> None:
        self._preview_temporary_file(temporary_path)
        self._write_log(
            f"已生成缓存文件：{temporary_path}\n"
            "请在打开的阅读器中使用“另存为”自行选择保存位置；程序不会弹出另存为窗口。\n"
        )

    def _preview_temporary_file(self, path: Path) -> None:
        """Open a safe local preview without invoking an unregistered association."""
        if os.name != "nt":
            return
        try:
            if path.suffix.casefold() in {".md", ".markdown"}:
                subprocess.Popen(["notepad.exe", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            # ShellExecute can display a system error dialog when no file
            # association exists.  Probe the registered open command first.
            import winreg

            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path.suffix) as extension_key:
                file_type = winreg.QueryValue(extension_key, "")
            if not file_type:
                return
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{file_type}\\shell\\open\\command") as command_key:
                command = winreg.QueryValue(command_key, "")
            if not command:
                return
            os.startfile(path)  # type: ignore[attr-defined]
        except (OSError, FileNotFoundError, ImportError):
            # Preview is optional; saving the translated temporary file is not.
            return


def run_gui() -> int:
    root = tk.Tk()
    TranslationApp(root)
    root.mainloop()
    return 0
