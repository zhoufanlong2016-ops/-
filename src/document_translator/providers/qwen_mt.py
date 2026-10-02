"""DashScope Qwen-MT provider for one translation unit."""

from __future__ import annotations

import os
import re
import time
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
from document_translator.translation_rules import normalize_chinese_spacing, protect_for_translation, restore_after_translation, restore_markers_best_effort

from .translation_prompt import PROMPT_VERSION, compile_translation_policy, matched_glossary_entries
from .batch_limits import split_semantic_batches
from document_translator.translation_rules import source_name_constraints, validate_name_retention


_COMPATIBLE_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_ALLOWED_MODELS = frozenset({"qwen-mt-plus", "qwen-mt-flash"})
_QWEN_LANGUAGE_NAMES = {"zh": "Chinese", "zh-cn": "Chinese", "en": "English"}
_ANONYMOUS_NAME_RE = re.compile(r"([A-Za-z])某某|某某")


@dataclass(frozen=True, slots=True)
class QwenMTConfig:
    """Non-secret settings for the DashScope OpenAI-compatible API."""

    model: str = "qwen-mt-plus"
    api_key_env: str = "DASHSCOPE_API_KEY"
    timeout: float = 60.0
    requests_per_minute: int = 45
    # Qwen-MT is a translation endpoint rather than a structured JSON batch
    # endpoint.  Keep synthetic ID envelopes small enough that the model can
    # reproduce every boundary reliably.
    batch_input_characters: int = 0

    def __post_init__(self) -> None:
        if self.model not in _ALLOWED_MODELS:
            raise ValueError("model must be qwen-mt-plus or qwen-mt-flash")
        if not self.api_key_env.strip():
            raise ValueError("api_key_env must not be empty")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 1 <= self.requests_per_minute <= 60:
            raise ValueError("requests_per_minute must be between 1 and 60")
        if self.batch_input_characters < 0:
            raise ValueError("batch_input_characters must not be negative")


class QwenMTError(RuntimeError):
    """A provider failure with a stable, non-secret-bearing error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class QwenMTProvider:
    """Translate a single unit through DashScope's compatible chat API."""

    provider_name = "qwen_mt"
    # Qwen-MT's native endpoint returns plain translated text and does not
    # guarantee preservation of synthetic stable-ID markers.  Keep its
    # translate_batch method for explicit callers/tests, but document routes
    # must use the native single-message protocol instead of fake envelopes.
    supports_stable_batch = False
    prompt_version = PROMPT_VERSION
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
        # Model Studio publishes a 60 RPM limit for qwen-mt-flash.  Keep a
        # safety margin because the key can also be used by another process.
        self._minimum_request_interval = 60.0 / self.config.requests_per_minute
        self._next_request_at = 0.0

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        api_key = os.getenv(self.config.api_key_env)
        if not api_key or not api_key.strip():
            raise QwenMTError("API_KEY_MISSING", "DashScope API key is not configured")

        policy = compile_translation_policy(unit, self._glossary)
        translation_options: dict[str, object] = {
            "source_lang": self._qwen_language(unit.source_language),
            "target_lang": self._qwen_language(unit.target_language),
            "domains": policy.qwen_domain,
        }
        # Qwen-MT has an official term-intervention field.  Use it to lock
        # numbers, units, standards and identifiers verbatim instead of adding
        # artificial placeholders to the source text.
        if policy.required_terms:
            translation_options["terms"] = [
                {"source": source, "target": target} for source, target in policy.required_terms
            ]
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "enable_thinking": False,
            "stream": False,
            "messages": [{"role": "user", "content": unit.source_text}],
            "translation_options": translation_options,
        }
        translation, request_count = self._request_translation(api_key, payload)

        residual_source_script = self._is_english_target(unit) and bool(re.search(r"[\u3400-\u9fff]", translation))
        if residual_source_script:
            # Qwen-MT has no system message channel.  Use its documented
            # domain hint for one bounded automatic correction before the
            # result enters the normal protected-token validation path.
            strict_payload = dict(payload)
            strict_options = dict(translation_options)
            strict_options["domains"] = (
                f"{policy.qwen_domain}; output only {self._qwen_language(unit.target_language)}; "
                "do not retain any source-language words or characters"
            )
            strict_payload["translation_options"] = strict_options
            translation, correction_requests = self._request_translation(api_key, strict_payload)
            request_count += correction_requests
            translation = self._repair_anonymous_name_marker(translation, unit)
            residual_source_script = bool(re.search(r"[\u3400-\u9fff]", translation))
        translation = normalize_chinese_spacing(translation, unit.target_language)
        result = TranslationResult(
            unit_id=unit.id,
            translation=translation,
            provider=self.provider_name,
            model=self.config.model,
            prompt_version=self.prompt_version,
            glossary_version=self.glossary_version,
            source_hash=sha256_text(unit.source_text),
            result_hash=sha256_text(translation),
            request_count=request_count,
            error="RESIDUAL_SOURCE_SCRIPT: Chinese characters remain in English output" if residual_source_script else None,
            validation_status="needs_review" if residual_source_script else "valid",
        )
        if validate_result_for_unit(unit, result):
            if validate_name_retention(unit.source_text, result.translation, unit.source_language, unit.target_language):
                strict_payload = dict(payload)
                strict_payload["translation_options"] = {
                    **translation_options,
                    "domains": policy.qwen_domain + " AUTOMATIC CORRECTION: " + "; ".join(validate_result_for_unit(unit, result)),
                }
                repaired, repair_requests = self._request_translation(api_key, strict_payload)
            else:
                repaired, repair_requests = self._translate_source_gaps(api_key, payload, unit)
            repaired = self._repair_anonymous_name_marker(repaired, unit)
            request_count += repair_requests
            result = result.model_copy(update={
                "translation": repaired, "result_hash": sha256_text(repaired), "request_count": request_count,
                "error": None, "validation_status": "valid",
            })
            errors = validate_result_for_unit(unit, result)
            if errors:
                raise QwenMTError(
                    "VALIDATION_FAILED",
                    "translation result failed validation after one repair: " + "; ".join(errors),
                )
        return result

    @staticmethod
    def _repair_anonymous_name_marker(translation: str, unit: TranslationUnit) -> str:
        """Convert the deterministic Chinese anonymous-name marker to English.

        Chinese disciplinary/legal documents use ``某某`` as a redacted
        surname/given-name marker.  Qwen-MT-Flash can leave it attached to an
        otherwise transliterated name (for example ``Lei某某``).  ``Moumou``
        is the fixed English rendering; no other Chinese text is rewritten.
        """

        if not QwenMTProvider._is_english_target(unit):
            return translation

        def replace(match: re.Match[str]) -> str:
            prefix = match.group(1)
            return f"{prefix} Moumou" if prefix else "Moumou"

        return _ANONYMOUS_NAME_RE.sub(replace, translation)

    def _translate_source_gaps(
        self, api_key: str, payload: dict[str, object], unit: TranslationUnit,
    ) -> tuple[str, int]:
        """One bounded Qwen recovery: translate gaps, restore source literals locally."""
        protected = protect_for_translation(unit.source_text, unit.protected_tokens)
        if not protected.replacements:
            raise QwenMTError("VALIDATION_FAILED", "no source gaps are available for repair")
        literals = dict(protected.replacements)
        marker_pattern = r"(\[\[TRP_\d{4}\]\])"
        repaired: list[str] = []
        request_count = 0
        for part in re.split(marker_pattern, protected.text):
            if part in literals:
                repaired.append(literals[part])
                continue
            if not self._contains_source_script(part, unit.source_language):
                repaired.append(part)
                continue
            fragment_payload = dict(payload)
            fragment_payload["messages"] = [{"role": "user", "content": part.strip()}]
            options = dict(payload["translation_options"])
            terms = [
                entry for entry in options.get("terms", [])
                if isinstance(entry, dict) and entry.get("source") in part
                and not str(entry.get("source", "")).startswith("[[TRP_")
            ]
            if terms:
                options["terms"] = terms
            else:
                options.pop("terms", None)
            fragment_payload["translation_options"] = options
            translated, calls = self._request_translation(api_key, fragment_payload)
            request_count += calls
            if "[[TRP_" in translated:
                raise QwenMTError("REPAIR_FAILED", "source-gap translation returned a protection marker")
            repaired.append(" " + translated.strip() + " ")
        return "".join(repaired).strip(), request_count

    @staticmethod
    def _contains_source_script(text: str, source_language: str) -> bool:
        """Decide whether a protected-token gap still needs translation."""
        source = source_language.strip().casefold()
        if source in {"zh", "zh-cn", "zh-hans", "zh-sg", "chinese"}:
            return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
        return bool(re.search(r"[A-Za-z]{2,}", text))

    def _request_translation(self, api_key: str, payload: dict[str, object]) -> tuple[str, int]:
        request_count = 0
        for attempt in range(4):
            try:
                self._wait_for_request_slot()
                response = self.client.post(
                    f"{_COMPATIBLE_ENDPOINT}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"}, json=payload,
                    timeout=self.config.timeout,
                )
                response.raise_for_status()
                translation = self._response_content(response.json()).strip()
                request_count += 1
                if not translation:
                    raise QwenMTError("EMPTY_TRANSLATION", "response translation must be non-empty text")
                return translation, request_count
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < 3:
                    request_count += 1
                    time.sleep(self._retry_delay(exc.response.headers.get("retry-after"), attempt))
                    continue
                code = "HTTP_429" if exc.response.status_code == 429 else f"HTTP_{exc.response.status_code}"
                raise QwenMTError(code, "DashScope request failed") from exc
            except httpx.HTTPError as exc:
                raise QwenMTError("HTTP_ERROR", "DashScope request failed") from exc
            except (ValueError, TypeError) as exc:
                raise QwenMTError("MALFORMED_RESPONSE", "DashScope returned an invalid response") from exc
        raise QwenMTError("HTTP_429", "DashScope rate limit exceeded after controlled retry")

    def _wait_for_request_slot(self) -> None:
        now = time.monotonic()
        if self._next_request_at > now:
            time.sleep(self._next_request_at - now)
        self._next_request_at = time.monotonic() + self._minimum_request_interval

    @staticmethod
    def _retry_delay(retry_after: str | None, attempt: int) -> float:
        try:
            return max(0.0, min(float(retry_after or ""), 60.0))
        except ValueError:
            return min(2.0 ** (attempt + 1), 30.0)

    def translate_batch(self, units: list[TranslationUnit]) -> list[TranslationResult]:
        """Translate semantic units in stable-ID batches; never network-loop per unit."""
        if not units:
            return []
        batches = split_semantic_batches(
            units, model=self.config.model,
            explicit_limit=self.config.batch_input_characters,
            overhead=32,
        )
        results: list[TranslationResult] = []
        for index, batch in enumerate(batches, start=1):
            print(f"qwen-mt batch {index}/{len(batches)}: units={len(batch)} chars={sum(len(unit.source_text) for unit in batch)}", flush=True)
            results.extend(self._translate_id_batch(batch))
        return results

    def _translate_id_batch(
        self,
        units: list[TranslationUnit],
        *,
        correction_requirements: dict[str, list[str]] | None = None,
    ) -> list[TranslationResult]:
        api_key = os.getenv(self.config.api_key_env)
        if not api_key or not api_key.strip():
            raise QwenMTError("API_KEY_MISSING", "DashScope API key is not configured")
        first = units[0]
        if any(u.source_language != first.source_language or u.target_language != first.target_language for u in units):
            raise QwenMTError("BATCH_LANGUAGE_MISMATCH", "a Qwen batch requires one language pair")
        policy = compile_translation_policy(first, self._glossary)
        terms_by_id = {
            unit.id: tuple((entry.source, entry.target) for entry in matched_glossary_entries(unit, self._glossary))
            if self._glossary is not None else ()
            for unit in units
        }
        batch_terms = list(policy.required_terms)
        for unit in units[1:]:
            batch_terms.extend(compile_translation_policy(unit, self._glossary).required_terms)
        batch_terms = list(dict.fromkeys(batch_terms))
        if batch_terms:
            policy = policy.__class__(instruction=policy.instruction + "\n" + "\n".join(f"{s} -> {t}" for s, t in batch_terms), qwen_domain=policy.qwen_domain + " Use these exact terms: " + "; ".join(f"{s}={t}" for s, t in batch_terms), required_terms=tuple(batch_terms))
        correction_text = ""
        if correction_requirements:
            correction_text = "\n\nAUTOMATIC CORRECTION. Return the same IDs and correct every listed defect. " \
                "Copy each required target term verbatim; do not omit, inflect, paraphrase, or replace it.\n" \
                + "\n".join(
                    f"{unit_id}: " + "; ".join(errors)
                    for unit_id, errors in correction_requirements.items()
                )
        protected_by_id = {
            unit.id: protect_for_translation(unit.source_text, unit.protected_tokens)
            for unit in units
        }
        content = correction_text + "\n\n".join(
            f"[[TRB:{index:06d}]]\n{protected_by_id[unit.id].text}\n[[/TRB:{index:06d}]]"
            for index, unit in enumerate(units)
        )
        translation_options: dict[str, object] = {
            "source_lang": self._qwen_language(first.source_language),
            "target_lang": self._qwen_language(first.target_language),
            "domains": policy.qwen_domain + "\nPer-item required_names (retain in the corresponding marker):\n" + "\n".join(
                f"[[TRB:{index:06d}]] {unit.id}: {source_name_constraints(unit.source_text, unit.source_language, unit.target_language)!r}"
                for index, unit in enumerate(units)
            ),
        }
        # The batch path must use the same structured term intervention as
        # translate_unit().  Putting terms only in the free-text domain hint
        # leaves the provider-side terminology constraint unenforced.
        if batch_terms:
            translation_options["terms"] = [
                {"source": source, "target": target} for source, target in batch_terms
            ]
        payload = {"model": self.config.model, "temperature": 0, "enable_thinking": False, "stream": False,
                   "messages": [{"role": "user", "content": content}],
                   "translation_options": translation_options}
        translated, calls = self._request_translation(api_key, payload)
        found = re.findall(r"\[\[TRB:(\d{6})\]\]\s*(.*?)\s*\[\[/TRB:\1\]\]", translated, flags=re.S)
        if not found and len(units) == 1:
            # A single semantic unit has an unambiguous mapping.  Some
            # Qwen-MT responses omit the synthetic outer markers entirely;
            # accept the raw translation, then apply the same strict
            # protected-token and glossary validation below.
            found = [("000000", translated)]
        if len(found) != len(units) or [int(i) for i, _ in found] != list(range(len(units))):
            raise QwenMTError("BATCH_MAPPING_INVALID", "Qwen batch response lost or reordered stable IDs")
        results = []
        invalid: dict[str, list[str]] = {}
        for unit, (_, text) in zip(units, found):
            translated_text = text.strip()
            try:
                translated_text = restore_after_translation(
                    translated_text, protected_by_id[unit.id],
                )
            except ValueError as error:
                translated_text = restore_markers_best_effort(translated_text, protected_by_id[unit.id])
                invalid[unit.id] = [f"PROTECTED_PLACEHOLDER_RESTORE_FAILED: {error}"]
                translated_text = text.strip()
            translated_text = normalize_chinese_spacing(translated_text, unit.target_language)
            result = TranslationResult(unit_id=unit.id, translation=translated_text, provider=self.provider_name, model=self.config.model,
                prompt_version=self.prompt_version, glossary_version=self.glossary_version, source_hash=sha256_text(unit.source_text),
                result_hash=sha256_text(translated_text), request_count=calls, validation_status="valid")
            errors = [
                *validate_result_for_unit(unit, result),
                *validate_glossary_terms(unit.source_text, result.translation, terms_by_id[unit.id]),
            ]
            if errors:
                invalid.setdefault(unit.id, []).extend(errors)
            results.append(result)
        if invalid:
            if correction_requirements is not None:
                if any(
                    error.startswith("PROTECTED_PLACEHOLDER_RESTORE_FAILED:")
                    for errors in invalid.values()
                    for error in errors
                ):
                    raise QwenMTError("BATCH_VALIDATION_FAILED", "; ".join(f"{key}: {value}" for key, value in invalid.items()))
                return [
                    item.model_copy(update={
                        "validation_status": "needs_review",
                        "error": "; ".join(invalid[item.unit_id]),
                    }) if item.unit_id in invalid else item
                    for item in results
                ]
            repair_units = [unit for unit in units if unit.id in invalid]
            repaired = self._translate_id_batch(repair_units, correction_requirements=invalid)
            repaired_by_id = {result.unit_id: result for result in repaired}
            results = [repaired_by_id.get(result.unit_id, result) for result in results]
        return results

    @staticmethod
    def _is_english_target(unit: TranslationUnit) -> bool:
        return unit.target_language.strip().casefold() in {"en", "en-us", "en-gb", "english"}

    @staticmethod
    def _message_content(unit: TranslationUnit) -> str:
        """Qwen-MT translates its sole message verbatim; send source only.

        This endpoint accepts neither a system role nor multiple messages.  If
        policy text is concatenated here, Qwen-MT translates that policy along
        with the document.  Immutable tokens are therefore enforced by the
        shared post-translation validation path instead.
        """
        return unit.source_text

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
