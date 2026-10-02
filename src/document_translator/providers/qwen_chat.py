"""DashScope general-Qwen Chat Completions provider."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from document_translator.core import (
    TranslationResult,
    TranslationUnit,
    sha256_text,
    validate_glossary_terms,
    validate_result_for_unit,
)
from document_translator.services.glossary import Glossary
from document_translator.translation_rules import (
    auto_correct_translation,
    protect_for_translation,
    restore_after_translation,
    restore_markers_best_effort,
)

from .translation_prompt import PROMPT_VERSION, compile_translation_policy, matched_glossary_entries
from document_translator.translation_rules import source_name_constraints
from .batch_limits import split_semantic_batches


_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


@dataclass(frozen=True, slots=True)
class QwenChatConfig:
    model: str
    api_key_env: str = "DASHSCOPE_API_KEY"
    timeout: float = 120.0
    # General Qwen models differ in how reliably they emit every JSON item;
    # keep the default conservative so one omitted item cannot invalidate a
    # long document batch.
    batch_input_characters: int = 0

    def __post_init__(self) -> None:
        if not self.model.strip() or self.model.casefold().startswith("qwen-mt"):
            raise ValueError("Qwen Chat requires a non-Qwen-MT model")
        if not self.api_key_env.strip() or self.timeout <= 0 or self.batch_input_characters < 0:
            raise ValueError("Qwen Chat configuration is invalid")


class QwenChatError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class QwenChatProvider:
    provider_name = "qwen"
    prompt_version = PROMPT_VERSION
    glossary_version = "none"

    def __init__(self, config: QwenChatConfig, *, client: httpx.Client, glossary: Glossary | None = None) -> None:
        self.config = config
        self.client = client
        self._glossary = glossary
        self.glossary_version = glossary.version if glossary is not None else "none"

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        return self.translate_batch([unit])[0]

    def translate_batch(self, units: list[TranslationUnit]) -> list[TranslationResult]:
        if not units:
            return []
        results: list[TranslationResult] = []
        for batch in split_semantic_batches(units, model=self.config.model, explicit_limit=self.config.batch_input_characters, overhead=64):
            results.extend(self._translate_with_split(batch))
        return results

    def _translate_with_split(self, units: list[TranslationUnit]) -> list[TranslationResult]:
        try:
            return self._translate_batch_once(units)
        except QwenChatError as exc:
            # Content-quality validation issues (residual English, untranslated
            # dates, missing proper names, ...) no longer raise here: they are
            # returned as validation_status="needs_review" results so one imperfect
            # unit cannot abort translation for the rest of the document.
            # What remains here is transport/mapping failures.
            if exc.code != "BATCH_MAPPING_INVALID":
                raise
            return self._translate_batch_once(units, mapping_retry=True)

    def _translate_batch_once(self, units: list[TranslationUnit], *, correction: dict[str, list[str]] | None = None, mapping_retry: bool = False) -> list[TranslationResult]:
        key = os.getenv(self.config.api_key_env)
        if not key or not key.strip():
            raise QwenChatError("API_KEY_MISSING", "DashScope API key is not configured")
        first = units[0]
        policy = compile_translation_policy(first, self._glossary)
        terms_by_id = {
            unit.id: tuple((entry.source, entry.target) for entry in matched_glossary_entries(unit, self._glossary))
            if self._glossary is not None else () for unit in units
        }
        batch_terms = list(policy.required_terms)
        for unit in units[1:]:
            batch_terms.extend(terms_by_id[unit.id])
        batch_terms = list(dict.fromkeys(batch_terms))
        terminology = "\n\nRequired terminology (mandatory):\n" + "\n".join(
            f"- {source} -> {target}" for source, target in batch_terms
        ) if batch_terms else ""
        correction_text = ""
        if mapping_retry:
            correction_text = "\n\nPROTOCOL CORRECTION. Return exactly one translation for every requested ID, with no omissions, duplicates, reordering, commentary, or markdown."
        if correction:
            correction_text = "\n\nAUTOMATIC CORRECTION. Fix every listed defect and return the same IDs.\n" + "\n".join(
                f"{unit_id}: {'; '.join(errors)}" for unit_id, errors in correction.items()
            )
        protected = {unit.id: protect_for_translation(unit.source_text, unit.protected_tokens) for unit in units}
        items = [{"id": unit.id, "text": protected[unit.id].text, "required_names": source_name_constraints(unit.source_text, unit.source_language, unit.target_language)} for unit in units]
        system = policy.instruction + terminology + correction_text + "\nReturn JSON only: {\"items\":[{\"id\":string,\"translation\":string}]}"
        body = {
            "model": self.config.model,
            "temperature": 0,
            # Document translation is a deterministic extraction/rewriting
            # task.  Disable hybrid reasoning so large batches do not spend
            # the request timeout in an internal thinking pass.
            "enable_thinking": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self.client.post(_ENDPOINT, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body, timeout=self.config.timeout)
            response.raise_for_status()
            content = self._content(response.json())
            payload = json.loads(content)
            rows = payload["items"]
            if not isinstance(rows, list):
                raise ValueError("items must be an array")
        except httpx.HTTPStatusError as exc:
            raise QwenChatError(f"HTTP_{exc.response.status_code}", "DashScope Qwen Chat request failed") from exc
        except httpx.HTTPError as exc:
            # Connection/TLS/timeout failures never reached a response body
            # at all -- collapsing them into the same "response was
            # invalid" message as a genuine malformed-JSON reply (below)
            # made a transient local network hiccup indistinguishable from
            # a real content/parsing defect while debugging a failure.
            raise QwenChatError("TRANSPORT_ERROR", f"DashScope Qwen Chat request failed: {exc}") from exc
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise QwenChatError("BATCH_FAILED", "DashScope Qwen Chat response was invalid") from exc
        mapped = {row.get("id"): row.get("translation") for row in rows if isinstance(row, dict)}
        expected = {unit.id for unit in units}
        if set(mapped) != expected or any(not isinstance(value, str) for value in mapped.values()):
            raise QwenChatError("BATCH_MAPPING_INVALID", "Qwen Chat batch IDs are incomplete")
        results: list[TranslationResult] = []
        invalid: dict[str, list[str]] = {}
        for unit in units:
            text = mapped[unit.id].strip()
            try:
                text = restore_after_translation(text, protected[unit.id])
            except ValueError as exc:
                text = restore_markers_best_effort(text, protected[unit.id])
                invalid[unit.id] = [f"PROTECTED_PLACEHOLDER_RESTORE_FAILED: {exc}"]
            else:
                text = auto_correct_translation(unit.source_text, text, unit.source_language, unit.target_language)
            result = TranslationResult(unit_id=unit.id, translation=text, provider=self.provider_name, model=self.config.model, prompt_version=self.prompt_version, glossary_version=self.glossary_version, source_hash=sha256_text(unit.source_text), result_hash=sha256_text(text), request_count=1, validation_status="valid")
            errors = [*validate_result_for_unit(unit, result), *validate_glossary_terms(unit.source_text, text, terms_by_id[unit.id])]
            if errors:
                invalid.setdefault(unit.id, []).extend(errors)
            results.append(result)
        if invalid:
            if correction is not None:
                # One automatic-correction retry has already run and the unit
                # is still imperfect. Keep the best-effort translation rather
                # than aborting the whole batch/document: flag it as a
                # warning so the caller can surface it in the report instead
                # of losing every other correctly translated unit.
                marked: list[TranslationResult] = []
                for result in results:
                    unit_errors = invalid.get(result.unit_id)
                    if unit_errors:
                        result = result.model_copy(update={
                            "validation_status": "needs_review",
                            "error": "; ".join(unit_errors),
                        })
                    marked.append(result)
                return marked
            repaired = self._translate_batch_once([unit for unit in units if unit.id in invalid], correction=invalid)
            repaired_by_id = {item.unit_id: item for item in repaired}
            return [repaired_by_id.get(item.unit_id, item) for item in results]
        return results

    @staticmethod
    def _content(body: Any) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("response has no chat content") from exc
        if not isinstance(content, str) or not content.strip():
            raise ValueError("response content must be non-empty text")
        return content.strip().removeprefix("```json").removesuffix("```").strip()
