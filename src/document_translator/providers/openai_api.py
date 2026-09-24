"""Official OpenAI Responses API provider for one translation unit."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from document_translator.core import TranslationResult, TranslationUnit, sha256_text, validate_glossary_terms, validate_result_for_unit
from document_translator.services.glossary import Glossary
from document_translator.translation_rules import protect_for_translation, restore_after_translation

from .translation_prompt import PROMPT_VERSION, compile_translation_policy, matched_glossary_entries


_ENDPOINT = "https://api.openai.com/v1/responses"
_ALLOWED_MODELS = frozenset({"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.4", "gpt-4o", "gpt-4o-mini"})


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    timeout: float = 120.0

    def __post_init__(self) -> None:
        if self.model not in _ALLOWED_MODELS:
            raise ValueError("unsupported OpenAI translation model")
        if not self.api_key_env.strip() or self.timeout <= 0:
            raise ValueError("OpenAI configuration is invalid")


class OpenAIProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OpenAIProvider:
    provider_name = "openai"
    prompt_version = PROMPT_VERSION
    glossary_version = "none"

    def __init__(
        self,
        config: OpenAIConfig,
        *,
        client: httpx.Client,
        glossary: Glossary | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self._glossary = glossary
        self.glossary_version = glossary.version if glossary is not None else "none"

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        key = os.getenv(self.config.api_key_env)
        if not key or not key.strip():
            raise OpenAIProviderError("API_KEY_MISSING", "OpenAI API key is not configured")
        policy = compile_translation_policy(unit, self._glossary)
        instruction = policy.instruction + "\n\n" + unit.source_text
        try:
            response = self.client.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": self.config.model, "reasoning": {"effort": "none"}, "input": instruction},
                timeout=self.config.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenAIProviderError(f"HTTP_{exc.response.status_code}", "OpenAI request failed") from exc
        except httpx.HTTPError as exc:
            raise OpenAIProviderError("HTTP_ERROR", "OpenAI request failed") from exc
        try:
            translation = self._output_text(response.json()).strip()
        except (TypeError, ValueError) as exc:
            raise OpenAIProviderError("MALFORMED_RESPONSE", "OpenAI response does not contain output text") from exc
        if not translation:
            raise OpenAIProviderError("EMPTY_TRANSLATION", "OpenAI response has empty translation")
        result = TranslationResult(
            unit_id=unit.id, translation=translation, provider=self.provider_name, model=self.config.model,
            prompt_version=self.prompt_version, glossary_version=self.glossary_version,
            source_hash=sha256_text(unit.source_text), result_hash=sha256_text(translation),
            request_count=1, validation_status="valid",
        )
        if validate_result_for_unit(unit, result):
            raise OpenAIProviderError("VALIDATION_FAILED", "OpenAI result failed validation")
        return result

    def translate_batch(self, units: list[TranslationUnit]) -> list[TranslationResult]:
        if not units:
            return []
        try:
            return self._translate_batch_once(units)
        except OpenAIProviderError as exc:
            # A provider may truncate or omit an item in an otherwise valid
            # large response.  Split only this integrity-failing batch, never
            # the normal request path, and keep semantic units intact.
            if exc.code not in {"BATCH_MAPPING_INVALID", "BATCH_VALIDATION_FAILED"} or len(units) == 1:
                raise
            midpoint = len(units) // 2
            return self.translate_batch(units[:midpoint]) + self.translate_batch(units[midpoint:])

    def _translate_batch_once(
        self,
        units: list[TranslationUnit],
        *,
        correction_requirements: dict[str, list[str]] | None = None,
    ) -> list[TranslationResult]:
        key = os.getenv(self.config.api_key_env)
        if not key or not key.strip(): raise OpenAIProviderError("API_KEY_MISSING", "OpenAI API key is not configured")
        first = units[0]
        # Use structured IDs for batch mapping. Do not inject custom inline
        # markers: mature translation APIs do not guarantee that arbitrary
        # placeholders are echoed. Immutable literals are checked
        # deterministically after the response instead.
        items = [{"id": unit.id, "text": unit.source_text} for unit in units]
        schema={"type":"object","properties":{"items":{"type":"array","items":{"type":"object","properties":{"id":{"type":"string"},"translation":{"type":"string"}},"required":["id","translation"],"additionalProperties":False}}},"required":["items"],"additionalProperties":False}
        policy = compile_translation_policy(first, self._glossary)
        terms_by_id = {
            unit.id: tuple((entry.source, entry.target) for entry in matched_glossary_entries(unit, self._glossary))
            if self._glossary is not None else ()
            for unit in units
        }
        batch_terms: list[tuple[str, str]] = list(policy.required_terms)
        if self._glossary is not None:
            for unit in units[1:]:
                batch_terms.extend((entry.source, entry.target) for entry in matched_glossary_entries(unit, self._glossary))
        batch_terms = list(dict.fromkeys(batch_terms))
        terminology = "\n\nRequired terminology for this entire batch:\n" + "\n".join(
            f"- {source} -> {target}" for source, target in batch_terms
        ) if batch_terms else ""
        correction = ""
        if correction_requirements:
            correction = "\n\nAUTOMATIC CORRECTION. Return the same IDs and fix every listed defect. " \
                "Copy each required target term verbatim; do not omit, inflect, paraphrase, or replace it.\n" \
                + "\n".join(
                    f"{unit_id}: " + "; ".join(errors)
                    for unit_id, errors in correction_requirements.items()
                )
        prompt = policy.instruction + terminology + correction + "\nReturn one translation for every item, preserving IDs.\n" + json.dumps({"items":items},ensure_ascii=False)
        try:
            response=self.client.post(_ENDPOINT,headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},json={"model":self.config.model,"input":prompt,"text":{"format":{"type":"json_schema","name":"translation_batch","strict":True,"schema":schema}}},timeout=self.config.timeout); response.raise_for_status()
            rows=json.loads(self._output_text(response.json()))["items"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc: raise OpenAIProviderError("BATCH_FAILED","OpenAI batch request failed") from exc
        mapped={row["id"]:row["translation"] for row in rows}
        if len(mapped)!=len(units) or set(mapped)!={u.id for u in units}: raise OpenAIProviderError("BATCH_MAPPING_INVALID","OpenAI batch IDs are incomplete")
        results=[]
        invalid: dict[str, list[str]] = {}
        for unit in units:
            text=mapped[unit.id]; result=TranslationResult(unit_id=unit.id,translation=text,provider=self.provider_name,model=self.config.model,prompt_version=self.prompt_version,glossary_version=self.glossary_version,source_hash=sha256_text(unit.source_text),result_hash=sha256_text(text),request_count=1,validation_status="valid")
            errors = [
                *validate_result_for_unit(unit, result),
                *validate_glossary_terms(unit.source_text, result.translation, terms_by_id[unit.id]),
            ]
            if errors:
                invalid[unit.id] = errors
            results.append(result)
        if invalid:
            if correction_requirements is not None:
                raise OpenAIProviderError("BATCH_VALIDATION_FAILED", "; ".join(f"{key}: {value}" for key, value in invalid.items()))
            repair_units = [unit for unit in units if unit.id in invalid]
            repaired = self._translate_batch_once(repair_units, correction_requirements=invalid)
            repaired_by_id = {result.unit_id: result for result in repaired}
            results = [repaired_by_id.get(result.unit_id, result) for result in results]
        return results

    @staticmethod
    def _output_text(body: Any) -> str:
        if not isinstance(body, dict) or not isinstance(body.get("output"), list):
            raise ValueError("response must contain output")
        for message in body["output"]:
            if not isinstance(message, dict) or message.get("type") != "message":
                continue
            for content in message.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
        raise ValueError("response has no output_text")
