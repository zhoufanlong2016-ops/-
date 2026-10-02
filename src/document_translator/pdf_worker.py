"""Project-owned PDF worker entry point.

Runs BabelDOC in debug/main-process mode inside this dedicated process.  This
avoids pdf2zh-next's nested Windows multiprocessing layer while keeping the
translation isolated from the CLI process.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path


_SKIP_TRANSLATION_PAGES: tuple[int, ...] = ()


def _install_layout_policy() -> None:
    from babeldoc.translator.translator import OpenAITranslator

    _disable_nested_provider_retries(OpenAITranslator)
    _disable_nested_pdf_save_process()
    _install_translation_stats_event()


def _disable_nested_pdf_save_process() -> None:
    """Avoid BabelDOC's 120-second nested clean-save timeout in the worker.

    The project already runs BabelDOC inside an isolated worker and validates
    the resulting PDF after it returns.  Spawning another Windows process for
    MuPDF's clean save can remain alive until BabelDOC's hard timeout when the
    worker is frozen by PyInstaller.  A direct non-clean save preserves the
    PDF content; the project-level structural/CMap gates still run afterward.
    """
    from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater

    if getattr(PDFCreater, "_document_translator_direct_save", False):
        return

    def direct_save(
        pdf,
        output_path,
        translation_config,
        garbage=1,
        deflate=True,
        clean=True,
        deflate_fonts=True,
        linear=False,
        timeout=120,
        tag="",
    ):
        pdf.save(
            output_path,
            garbage=garbage,
            deflate=deflate,
            clean=False,
            deflate_fonts=deflate_fonts,
            linear=linear,
        )
        return False

    PDFCreater.save_pdf_with_timeout = staticmethod(direct_save)
    PDFCreater._document_translator_direct_save = True


def _install_translation_stats_event() -> None:
    """Emit aggregate BabelDOC fallback counters without exposing document text."""
    from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly

    if getattr(ILTranslatorLLMOnly, "_document_translator_stats_event", False):
        return
    original = ILTranslatorLLMOnly.translate

    def translate_with_stats(self, docs, *args, **kwargs):
        try:
            return original(self, docs, *args, **kwargs)
        finally:
            print(
                json.dumps(
                    {
                        "type": "translation_stats",
                        "successful_paragraphs": self.ok_count,
                        "fallback_paragraphs": self.fallback_count,
                        "translated_paragraphs": self.total_count,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    ILTranslatorLLMOnly.translate = translate_with_stats
    ILTranslatorLLMOnly._document_translator_stats_event = True


def _install_large_semantic_batches() -> None:
    """Raise BabelDOC's very small default page-batch thresholds.

    BabelDOC 0.18 splits ordinary page content after 200 tokens or six
    paragraphs.  For a three-page policy document that turns a single page
    into many independent gateway calls and makes provider latency dominate
    the job.  Its BatchParagraph path already carries a stable ID for every
    paragraph and validates the returned mapping, so increasing the bounded
    semantic batch does not weaken result integrity.
    """
    from babeldoc.format.pdf.document_il.midend import il_translator_llm_only as module
    from babeldoc.format.pdf.high_level import ILTranslatorLLMOnly as high_level_translator

    translator = module.ILTranslatorLLMOnly
    if getattr(translator, "_document_translator_large_batches", False):
        return

    batch_paragraph = module.BatchParagraph
    is_cid = module.is_cid_paragraph
    is_numeric = module.is_pure_numeric_paragraph
    is_placeholder = module.is_placeholder_only_paragraph

    def process_page(
        self,
        page,
        executor,
        pbar=None,
        tracker=None,
        executor2=None,
        translated_ids=None,
    ):
        self.translation_config.raise_if_cancelled()
        if getattr(page, "page_number", None) in _SKIP_TRANSLATION_PAGES:
            # BabelDOC's IL stores only horizontal/vertical text orientation;
            # preserve arbitrary-angle drawing pages as source content rather
            # than flattening their labels into misplaced horizontal text.
            for paragraph in page.pdf_paragraph:
                if pbar:
                    pbar.advance(1)
            return
        page_font_map = {font.font_id: font for font in page.pdf_font}
        page_xobj_font_map = {}
        for xobj in page.pdf_xobject:
            page_xobj_font_map[xobj.xobj_id] = page_font_map.copy()
            for font in xobj.pdf_font:
                page_xobj_font_map[xobj.xobj_id][font.font_id] = font

        paragraphs = []
        total_token_count = 0
        for paragraph in page.pdf_paragraph:
            if id(paragraph) in translated_ids:
                continue
            if paragraph.debug_id is None or paragraph.unicode is None:
                continue
            if (
                is_cid(paragraph)
                or len(paragraph.unicode) < self.translation_config.min_text_length
                or is_numeric(paragraph)
                or is_placeholder(paragraph)
            ):
                if pbar:
                    pbar.advance(1)
                continue

            total_token_count += self.calc_token_count(paragraph.unicode)
            paragraphs.append(paragraph)
            translated_ids.add(id(paragraph))
            if paragraph.layout_label == "title":
                self.shared_context_cross_split_part.recent_title_paragraph = (
                    self.shared_context_cross_split_part.snapshot_title_paragraph(paragraph)
                )

            # Bounded by both content and item count; this is one semantic
            # request, never a per-paragraph network loop.
            if total_token_count > 900 or len(paragraphs) >= 24:
                self.mid += 1
                executor.submit(
                    self.translate_paragraph,
                    batch_paragraph(paragraphs, [page] * len(paragraphs), tracker),
                    pbar,
                    page_font_map,
                    page_xobj_font_map,
                    self.translation_config.shared_context_cross_split_part.first_paragraph,
                    self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                    executor2,
                    priority=1048576 - total_token_count,
                    paragraph_token_count=total_token_count,
                    mp_id=self.mid,
                )
                paragraphs = []
                total_token_count = 0

        if paragraphs:
            self.mid += 1
            executor.submit(
                self.translate_paragraph,
                batch_paragraph(paragraphs, [page] * len(paragraphs), tracker),
                pbar,
                page_font_map,
                page_xobj_font_map,
                self.translation_config.shared_context_cross_split_part.first_paragraph,
                self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                executor2,
                priority=1048576 - total_token_count,
                paragraph_token_count=total_token_count,
                mp_id=self.mid,
            )

    translator.process_page = process_page
    # high_level imports the class directly, so update that reference too.
    module.ILTranslatorLLMOnly._document_translator_large_batches = True
    if high_level_translator is not translator:
        high_level_translator.process_page = process_page


def _disable_nested_provider_retries(translator: object) -> None:
    """Keep retry ownership in the project gateway.

    BabelDOC decorates both OpenAI translation methods with a 100-attempt
    RateLimitError retry policy.  The project gateway already performs the
    bounded upstream retry and backoff, so retaining BabelDOC's policy can
    leave a worker waiting for many minutes after one malformed/throttled
    provider response.  Calling the decorator's original function once makes
    the worker fail fast and lets the gateway's controlled result propagate.
    """

    for name in ("do_translate", "do_llm_translate"):
        current = getattr(translator, name)
        original = getattr(current, "__wrapped__", None)
        if original is None or getattr(current, "_document_translator_single_attempt", False):
            continue

        def single_attempt(self, *args, __original=original, **kwargs):
            return __original(self, *args, **kwargs)

        single_attempt._document_translator_single_attempt = True
        setattr(translator, name, single_attempt)


def _pdf_runtime_limits() -> tuple[int, int]:
    """Return bounded PDF request rate and worker concurrency.

    The old fixed ``1/1`` settings serialized every paragraph even though
    the local gateway is threaded.  Keep a conservative default while
    allowing operators to lower it for a stricter upstream quota.
    """
    def bounded(name: str, default: int) -> int:
        try:
            return max(1, min(8, int(os.environ.get(name, str(default)))))
        except (TypeError, ValueError):
            return default

    qps = bounded("DOCUMENT_TRANSLATOR_PDF_QPS", 6)
    workers = bounded("DOCUMENT_TRANSLATOR_PDF_WORKERS", 6)
    return qps, min(qps, workers)


async def _translate(payload: dict[str, object]) -> None:
    global _SKIP_TRANSLATION_PAGES
    _SKIP_TRANSLATION_PAGES = tuple(
        int(page) for page in payload.get("skip_translation_pages", ())
    )
    from pdf2zh_next.config.model import BasicSettings, PDFSettings, SettingsModel, TranslationSettings
    from pdf2zh_next.config.translate_engine_model import OpenAISettings
    from babeldoc.format.pdf.high_level import async_translate as babeldoc_translate
    from pdf2zh_next.high_level import create_babeldoc_config

    _install_layout_policy()
    qps, pool_max_workers = _pdf_runtime_limits()

    target_language = str(payload["target_language"])
    settings = SettingsModel(
        # The project-owned subprocess already provides process isolation.
        # Keep BabelDOC debug labels disabled; the worker invokes BabelDOC
        # directly below so pdf2zh-next does not create another Windows
        # multiprocessing layer.
        basic=BasicSettings(debug=False),
        translation=TranslationSettings(
            lang_in=str(payload["source_language"]),
            lang_out=target_language,
            output=str(payload["workdir"]),
            qps=qps,
            pool_max_workers=pool_max_workers,
            min_text_length=1,
            no_auto_extract_glossary=True,
            glossaries=payload.get("glossary"),
        ),
        pdf=PDFSettings(
            no_dual=True,
            no_mono=False,
            watermark_output_mode="no_watermark",
            translate_table_text=False,
        ),
        translate_engine_settings=OpenAISettings(
            openai_model=str(payload["model"]),
            openai_base_url=str(payload["gateway_url"]),
            openai_api_key="gateway-local",
            # The gateway can perform three bounded 180-second upstream
            # attempts.  Keep the worker timeout slightly above that window
            # so the client does not abort before the gateway returns its
            # controlled final result.
            openai_timeout="600",
            openai_enable_json_mode=True,
            openai_send_temprature=False,
            openai_send_reasoning_effort=False,
        ),
    )
    from document_translator.providers.translation_prompt import general_translation_instruction

    settings.translation.custom_system_prompt = (
        general_translation_instruction(str(payload["source_language"]), target_language)
        + "\n\nPDF batch contract: return only the required JSON array. Preserve every "
        "stable id and return exactly one output for each input item. Do not "
        "merge, omit, reorder, summarize, or invent items. Numbers, decimal "
        "precision, signs, ranges, dates, units, drawing references, identifiers "
        "and supplied terminology are immutable and must be copied exactly. "
        "ASCII hyphen-minus U+002D must remain unchanged inside identifiers."
    )
    source = Path(str(payload["source"]))
    settings.validate_settings()
    babeldoc_config = create_babeldoc_config(settings, source)
    async for event in babeldoc_translate(babeldoc_config):
        if not isinstance(event, dict):
            continue
        if event.get("type") == "finish":
            result = event.get("translate_result")
            event = {
                "type": "finish",
                "translate_result": {
                    key: str(getattr(result, key))
                    for key in ("mono_pdf_path", "no_watermark_mono_pdf_path")
                    if getattr(result, key, None)
                },
                "token_usage": event.get("token_usage"),
            }
        print(json.dumps(event, ensure_ascii=False, default=str), flush=True)
        if event.get("type") == "finish":
            break


def main() -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        # Read the request as bytes so Windows text-mode stdin cannot
        # reinterpret backslashes in absolute paths before JSON decoding.
        raw_request = getattr(sys.stdin, "buffer", sys.stdin).read()
        if isinstance(raw_request, bytes):
            raw_request = raw_request.decode("utf-8")
        payload = json.loads(raw_request)
        asyncio.run(_translate(payload))
        return 0
    except Exception as error:
        print(json.dumps({"type": "error", "error": f"{error}\n{traceback.format_exc()}"}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
