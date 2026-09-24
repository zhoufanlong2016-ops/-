"""Project-owned PDF worker entry point.

Runs BabelDOC in debug/main-process mode inside this dedicated process.  This
avoids pdf2zh-next's nested Windows multiprocessing layer while keeping the
translation isolated from the CLI process.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path


def _install_layout_policy() -> None:
    from babeldoc.translator.translator import OpenAITranslator

    _disable_nested_provider_retries(OpenAITranslator)


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


async def _translate(payload: dict[str, object]) -> None:
    from pdf2zh_next.config.model import BasicSettings, PDFSettings, SettingsModel, TranslationSettings
    from pdf2zh_next.config.translate_engine_model import OpenAISettings
    from babeldoc.format.pdf.high_level import async_translate as babeldoc_translate
    from pdf2zh_next.high_level import create_babeldoc_config

    _install_layout_policy()

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
            qps=1,
            pool_max_workers=1,
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
    settings.translation.custom_system_prompt = (
        "Document Translator PDF gateway v5. Translate every paragraph into "
        f"{target_language}; preserve IDs, codes, numbers, units, placeholders, "
        "and tags exactly. For a document reference, translate the "
        "natural-language issuer and reference marker; protect only its "
        "year, serial number, and ordering. Never copy source-script text "
        "into a Latin-target result. Preserve boundary whitespace and structural labels; "
        "the application will enforce the configured target style. Return "
        "only the required JSON array."
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
        payload = json.load(sys.stdin)
        asyncio.run(_translate(payload))
        return 0
    except Exception as error:
        print(json.dumps({"type": "error", "error": f"{error}\n{traceback.format_exc()}"}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
