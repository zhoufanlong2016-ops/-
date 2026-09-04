"""A narrow local llama-server provider for one translation unit."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from document_translator.core import (
    TranslationResult,
    TranslationUnit,
    sha256_text,
    validate_result_for_unit,
)
from document_translator.services.glossary import Glossary


@dataclass(frozen=True, slots=True)
class LocalLlamaConfig:
    """Connection settings for an OpenAI-compatible local llama-server."""

    endpoint: str = "http://127.0.0.1:8088"
    model: str = "local-model"
    timeout: float = 60.0
    response_mode: Literal["json", "plain_text"] = "json"

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise ValueError("endpoint must not be empty")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.response_mode not in {"json", "plain_text"}:
            raise ValueError("response_mode must be json or plain_text")


class LocalLlamaError(RuntimeError):
    """A provider failure with a stable, non-secret-bearing error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class LocalLlamaProvider:
    """Translate a single unit through llama-server's chat-completions API."""

    provider_name = "local_llama"
    prompt_version = "local-llama-v1"
    glossary_version = "none"
    _plain_text_prompt_markers = (
        "unit_id",
        "source_text",
        "json only",
        "json output",
        '"translation"',
        "return only the translation",
        "do not return json",
        "preserve every protected token",
        "<source>",
        "</source>",
    )
    def __init__(
        self,
        config: LocalLlamaConfig | None = None,
        *,
        client: httpx.Client,
        glossary: Glossary | None = None,
    ) -> None:
        self.config = config or LocalLlamaConfig()
        self.client = client
        self._glossary = glossary
        self.glossary_version = glossary.version if glossary is not None else "none"

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": self._prompt_for(unit),
                }
            ],
        }
        try:
            response = self.client.post(
                f"{self.config.endpoint.rstrip('/')}/v1/chat/completions",
                json=payload,
                timeout=self.config.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LocalLlamaError("HTTP_ERROR", "local llama-server request failed") from exc

        try:
            content = self._response_content(response.json())
        except (ValueError, TypeError) as exc:
            raise LocalLlamaError("MALFORMED_RESPONSE", "response does not contain model text") from exc

        if self.config.response_mode == "json":
            try:
                translated = json.loads(content)
            except json.JSONDecodeError as exc:
                raise LocalLlamaError("MALFORMED_RESPONSE", "response does not contain valid translation JSON") from exc
            if not isinstance(translated, dict) or set(translated) != {"unit_id", "translation"}:
                raise LocalLlamaError("INVALID_RESPONSE_SCHEMA", "translation JSON must contain only unit_id and translation")
            if translated["unit_id"] != unit.id:
                raise LocalLlamaError("UNIT_ID_MISMATCH", "response unit ID does not match request")
            translation = translated["translation"]
        else:
            translation = content.strip()
            lowered_translation = translation.casefold()
            if any(marker in lowered_translation for marker in self._plain_text_prompt_markers):
                raise LocalLlamaError("PROMPT_LEAKAGE", "plain-text response contains translation prompt content")

        if not isinstance(translation, str) or not translation.strip():
            raise LocalLlamaError("EMPTY_TRANSLATION", "response translation must be non-empty text")

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
        errors = validate_result_for_unit(unit, result)
        if errors:
            raise LocalLlamaError("VALIDATION_FAILED", "translation result failed validation")
        return result

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

    def _prompt_for(self, unit: TranslationUnit) -> str:
        protected = json.dumps(unit.protected_tokens, ensure_ascii=False)
        glossary_instruction = ""
        if self._glossary is not None:
            entries = self._glossary.entries_for(unit.source_text)
            if entries:
                mappings = "\n".join(
                    f"{entry.source} -> {entry.target}" for entry in entries
                )
                glossary_instruction = (
                    "\nUse these required terminology mappings exactly:\n" + mappings + "\n"
                )
        if self.config.response_mode == "plain_text":
            return (
                "Translate the source text from " + unit.source_language + " to " + unit.target_language + ".\n"
                "Return only the translation. Do not return JSON, labels, explanations, the source text, "
                "or any instruction text.\n"
                "Preserve every protected token exactly, including spelling and count: " + protected + ".\n"
                + glossary_instruction
                + "<source>\n" + unit.source_text + "\n</source>"
            )
        return (
            "Translate the source text from " + unit.source_language + " to " + unit.target_language + ".\n"
            "Return JSON only, with exactly these keys: unit_id and translation.\n"
            "Preserve every protected token exactly, including spelling and count: " + protected + ".\n"
            + glossary_instruction
            + "unit_id: " + unit.id + "\n"
            "source_text:\n" + unit.source_text
        )
