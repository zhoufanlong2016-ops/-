"""DashScope Qwen-MT provider for one translation unit."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from document_translator.core import (
    TranslationResult,
    TranslationUnit,
    sha256_text,
    validate_result_for_unit,
)
from document_translator.services.glossary import Glossary


_COMPATIBLE_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_ALLOWED_MODELS = frozenset({"qwen-mt-plus", "qwen-mt-flash"})
_QWEN_LANGUAGE_NAMES = {"zh": "Chinese", "zh-cn": "Chinese", "en": "English"}


@dataclass(frozen=True, slots=True)
class QwenMTConfig:
    """Non-secret settings for the DashScope OpenAI-compatible API."""

    model: str = "qwen-mt-plus"
    api_key_env: str = "DASHSCOPE_API_KEY"
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if self.model not in _ALLOWED_MODELS:
            raise ValueError("model must be qwen-mt-plus or qwen-mt-flash")
        if not self.api_key_env.strip():
            raise ValueError("api_key_env must not be empty")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")


class QwenMTError(RuntimeError):
    """A provider failure with a stable, non-secret-bearing error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class QwenMTProvider:
    """Translate a single unit through DashScope's compatible chat API."""

    provider_name = "qwen_mt"
    prompt_version = "qwen-mt-v1"
    glossary_version = "none"
    def __init__(
        self,
        config: QwenMTConfig | None = None,
        *,
        client: httpx.Client,
        glossary: Glossary | None = None,
    ) -> None:
        self.config = config or QwenMTConfig()
        self.client = client
        self._glossary = glossary
        self.glossary_version = glossary.version if glossary is not None else "none"

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        api_key = os.getenv(self.config.api_key_env)
        if not api_key or not api_key.strip():
            raise QwenMTError("API_KEY_MISSING", "DashScope API key is not configured")

        translation_options: dict[str, object] = {
            "source_lang": self._qwen_language(unit.source_language),
            "target_lang": self._qwen_language(unit.target_language),
        }
        if self._glossary is not None:
            entries = self._glossary.entries_for(unit.source_text)
            if entries:
                translation_options["terms"] = [
                    {"source": entry.source, "target": entry.target} for entry in entries
                ]
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": unit.source_text}],
            "translation_options": translation_options,
        }
        try:
            response = self.client.post(
                f"{_COMPATIBLE_ENDPOINT}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=self.config.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise QwenMTError(f"HTTP_{exc.response.status_code}", "DashScope request failed") from exc
        except httpx.HTTPError as exc:
            raise QwenMTError("HTTP_ERROR", "DashScope request failed") from exc

        try:
            translation = self._response_content(response.json()).strip()
        except (ValueError, TypeError) as exc:
            raise QwenMTError("MALFORMED_RESPONSE", "response does not contain translation text") from exc
        if not translation:
            raise QwenMTError("EMPTY_TRANSLATION", "response translation must be non-empty text")

        result = TranslationResult(
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
        if validate_result_for_unit(unit, result):
            raise QwenMTError("VALIDATION_FAILED", "translation result failed validation")
        return result

    @staticmethod
    def _qwen_language(language: str) -> str:
        return _QWEN_LANGUAGE_NAMES.get(language.strip().casefold(), language)

    @staticmethod
    def _response_content(body: Any) -> str:
        if not isinstance(body, dict):
            raise ValueError("response must be an object")
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("response must have one choice")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("response choice must have string message content")
        return message["content"]
