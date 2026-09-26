"""Explicit-provider gateway used by the BabelDOC PDF worker.

The worker speaks the OpenAI-compatible Chat Completions protocol.  This
small local gateway owns credentials and adapts that protocol to the selected
cloud API.  Keeping the adapter here gives the PDF route one auditable place
for provider selection, structured-output enforcement, control-character
sanitisation, and immutable-identifier protection.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from document_translator.translation_rules import source_name_constraints, validate_name_retention, validate_translation_residue, auto_correct_translation, rule_protected_tokens
from document_translator.core.validation import validate_placeholders

from .pdf_pipeline import (
    normalize_unicode_dashes,
    remove_control_characters,
    restore_immutable_identifiers,
)


_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_UPSTREAM_ATTEMPTS = 2
_UPSTREAM_TIMEOUT_SECONDS = 60
_TRANSIENT_UPSTREAM_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class GatewayError(RuntimeError):
    """An expected gateway failure with a stable, non-secret error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 502,
        upstream_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.upstream_code = upstream_code
        self.request_id = request_id
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    provider: str
    model: str
    endpoint: str
    credential_env: str

    @classmethod
    def from_provider(cls, *, provider: str, model: str) -> "GatewayConfig":
        model = (model or "").strip()
        if not model:
            raise ValueError("--model is required for PDF translation")
        if provider == "qwen":
            # Qwen-MT is a raw single-message machine-translation endpoint;
            # BabelDOC's PDF LLM path requires a JSON array batch.  General
            # Qwen models (qwen-plus/qwen-max and compatible variants) support
            # the structured chat contract used here.
            if model.casefold().startswith("qwen-mt"):
                raise ValueError(
                    "PDF Qwen translation requires a general Qwen model "
                    "(for example qwen-plus or qwen-max); qwen-mt models do "
                    "not support BabelDOC's structured batch contract"
                )
            return cls(
                "qwen",
                model,
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                "DASHSCOPE_API_KEY",
            )
        if provider == "gpt":
            return cls("gpt", model, "https://api.openai.com/v1/responses", "OPENAI_API_KEY")
        raise ValueError("PDF provider must be qwen or gpt")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self._send(400, {"error": "invalid content-length"})
            return
        if length < 0 or length > _MAX_REQUEST_BYTES:
            self._send(413, {"error": "request body is too large"})
            return

        config: GatewayConfig = self.server.gateway_config
        key = os.environ.get(config.credential_env)
        if not key or not key.strip():
            self._send(500, {"error": f"gateway credential {config.credential_env} is not configured"})
            return

        try:
            request_body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(request_body, dict):
                raise GatewayError("REQUEST_INVALID", "worker request must be a JSON object", status=400)
            if config.provider == "gpt":
                self._handle_gpt(config, key, request_body)
            else:
                self._handle_qwen(config, key, request_body)
        except GatewayError as exc:
            payload: dict[str, object] = {
                "error": exc.args[0],
                "error_type": exc.code,
            }
            if exc.upstream_code:
                payload["upstream_code"] = exc.upstream_code
            if exc.request_id:
                payload["request_id"] = exc.request_id
            self._send(exc.status, payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._send(400, {"error": str(exc) or "invalid worker request", "error_type": "REQUEST_INVALID"})
        except Exception:
            # Never return an upstream body or exception string: provider
            # responses can contain prompts, document text, or credentials.
            self._send(502, {"error": "upstream translation failed", "error_type": "UPSTREAM_FAILED"})

    def _handle_gpt(self, config: GatewayConfig, key: str, request_body: dict[str, Any]) -> None:
        # Translation batches do not need chain-of-thought.  Set the
        # model-aware low-cost default when the caller did not provide one.
        request_body = dict(request_body)
        model_name = config.model.casefold()
        if model_name.startswith("gpt-5") and not request_body.get("reasoning_effort"):
            request_body["reasoning_effort"] = "none"
        answer = _structured_answer_with_retry(
            request_body,
            lambda body: self._gpt_upstream_request(config, key, body),
        )
        self._send(200, _chat_completion(config.model, answer))

    def _handle_qwen(self, config: GatewayConfig, key: str, request_body: dict[str, Any]) -> None:
        # DashScope's general Qwen models expose the same Chat Completions
        # contract.  Sanitize every string before sending so control bytes in
        # extracted PDF text cannot invalidate a JSON batch.
        request_body = _with_name_constraints(request_body)
        self._audit_request("qwen", request_body)
        upstream_request = _sanitize_json(request_body)
        upstream_request["model"] = config.model
        # General Qwen reasoning models may otherwise spend several minutes
        # thinking before emitting a short translation batch.  The document
        # contract requires deterministic translation, not chain-of-thought.
        upstream_request["enable_thinking"] = False
        # Qwen3.8/Qwen3.7 also expose the OpenAI-compatible
        # ``reasoning_effort`` control.  Disabling ``enable_thinking`` alone
        # is insufficient for these models: without an explicit effort value
        # they can retain their very large default reasoning budget.
        upstream_request["reasoning_effort"] = "none"
        upstream_request["stream"] = False
        started = time.monotonic()
        try:
            response = _request_json(config.endpoint, key, upstream_request)
        except GatewayError as error:
            self._audit_result(
                "qwen",
                request_body,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                outcome="error",
                error_type=error.code,
            )
            raise
        response = _sanitize_json(response)
        if request_body.get("response_format"):
            normalised = _structured_answer_with_retry(
                request_body,
                lambda body: self._qwen_upstream_request(config, key, body),
                first_response=response,
            )
            response = _replace_chat_response_content(response, normalised)
        else:
            # Do not pass a provider body with a missing/invalid message
            # through to BabelDOC.  It otherwise waits for a usable text
            # result and appears as a long worker stall instead of a bounded
            # gateway failure.
            _chat_response_content(response)
        self._audit_result(
            "qwen",
            request_body,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            outcome="ok",
            response_type="chat_completion",
        )
        self._send(200, response)

    def _gpt_upstream_request(
        self, config: GatewayConfig, key: str, request_body: dict[str, Any]
    ) -> str:
        self._audit_request("gpt", request_body)
        return _responses_output_text(
            _request_json(
                config.endpoint,
                key,
                _messages_to_responses(request_body, model=config.model),
            )
        )

    def _qwen_upstream_request(
        self, config: GatewayConfig, key: str, request_body: dict[str, Any]
    ) -> str:
        self._audit_request("qwen", request_body)
        return _chat_response_content(
            _sanitize_json(
                _request_json(
                    config.endpoint,
                    key,
                    _sanitize_json(request_body),
                )
            )
        )

    def _audit_request(self, provider: str, request_body: dict[str, Any]) -> None:
        server = getattr(self, "server", None)
        audit_path = getattr(server, "gateway_audit_path", None)
        if not audit_path:
            return
        try:
            prompt = _last_user_text(request_body)
            contract = _extract_babeldoc_batch(prompt)
            batch_count = len(contract[1]) if contract is not None else None
            marker_found = "## Here is the input:" in prompt
            prompt_length = len(prompt)
        except Exception:
            batch_count = None
            marker_found = False
            prompt_length = None
        record = {
            "provider": provider,
            "model": request_body.get("model"),
            "batch_count": batch_count,
            "structured": bool(request_body.get("response_format")),
            "marker_found": marker_found,
            "prompt_length": prompt_length,
        }
        try:
            with server.gateway_audit_lock:
                path = Path(audit_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _audit_result(
        self,
        provider: str,
        request_body: dict[str, Any],
        *,
        elapsed_ms: int,
        outcome: str,
        response_type: str | None = None,
        error_type: str | None = None,
    ) -> None:
        server = getattr(self, "server", None)
        audit_path = getattr(server, "gateway_audit_path", None)
        if not audit_path:
            return
        record: dict[str, object] = {
            "event": "upstream_result",
            "provider": provider,
            "model": request_body.get("model"),
            "elapsed_ms": elapsed_ms,
            "outcome": outcome,
        }
        if response_type:
            record["response_type"] = response_type
        if error_type:
            record["error_type"] = error_type
        try:
            with server.gateway_audit_lock:
                path = Path(audit_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _send(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A worker can time out or cancel a request after the upstream
            # result is ready.  Do not emit a second traceback from the local
            # gateway in that normal cancellation path.
            return

    def log_message(self, *_args: object) -> None:
        return


class TranslationGateway:
    def __init__(
        self,
        *,
        config: GatewayConfig,
        host: str = "127.0.0.1",
        port: int = 0,
        audit_path: str | Path | None = None,
    ):
        self.server = ThreadingHTTPServer((host, port), _Handler)
        # A timed-out/cancelled upstream request must not keep the parent CLI
        # alive or hold the gateway port after the job has been failed closed.
        # The request itself is bounded by _request_json's timeout; daemon
        # handler threads provide a second safety net for cancellation paths.
        self.server.daemon_threads = True
        self.server.allow_reuse_address = True
        self.server.gateway_config = config
        self.server.gateway_audit_path = str(audit_path) if audit_path is not None else None
        self.server.gateway_audit_lock = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> "TranslationGateway":
        self.thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=5)


def _request_json(endpoint: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(_MAX_UPSTREAM_ATTEMPTS):
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urlopen(request, timeout=_UPSTREAM_TIMEOUT_SECONDS) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Keep the status for diagnostics but discard the response body,
            # which may echo document content or provider request details.
            if exc.code in _TRANSIENT_UPSTREAM_STATUS and attempt + 1 < _MAX_UPSTREAM_ATTEMPTS:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(_retry_delay(retry_after, attempt))
                continue
            upstream_code, request_id = _safe_upstream_diagnostics(exc)
            suffix = f" ({upstream_code})" if upstream_code else ""
            raise GatewayError(
                f"UPSTREAM_HTTP_{exc.code}",
                f"upstream returned HTTP {exc.code}{suffix}",
                upstream_code=upstream_code,
                request_id=request_id,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt + 1 < _MAX_UPSTREAM_ATTEMPTS:
                time.sleep(_retry_delay(None, attempt))
                continue
            raise GatewayError("UPSTREAM_NETWORK", "upstream network request failed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise GatewayError("UPSTREAM_INVALID_JSON", "upstream returned invalid JSON") from exc
        break
    else:  # pragma: no cover - the loop always returns or raises
        raise GatewayError("UPSTREAM_NETWORK", "upstream network request failed")
    if not isinstance(decoded, dict):
        raise GatewayError("UPSTREAM_INVALID_JSON", "upstream response must be a JSON object")
    return decoded


def _safe_upstream_diagnostics(error: HTTPError) -> tuple[str | None, str | None]:
    """Extract only allow-listed provider diagnostics from an HTTP error.

    Provider response bodies can contain prompts, translated text, or other
    sensitive material.  Keep only a short machine-readable error code and a
    request identifier so operators can distinguish billing, authentication,
    quota, and permission failures without leaking document content.
    """
    request_id: str | None = None
    for header in ("x-request-id", "x-dashscope-request-id", "request-id"):
        value = error.headers.get(header) if error.headers else None
        if value and len(value) <= 128:
            request_id = str(value)
            break
    upstream_code: str | None = None
    try:
        raw = error.read(4096)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
            nested = payload.get("error")
            if isinstance(nested, dict):
                candidate = nested.get("code") or nested.get("type")
            else:
                candidate = payload.get("code") or payload.get("type")
            if isinstance(candidate, str) and 0 < len(candidate) <= 128:
                upstream_code = candidate
            body_request_id = payload.get("request_id") or payload.get("requestId")
            if request_id is None and isinstance(body_request_id, str) and len(body_request_id) <= 128:
                request_id = body_request_id
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return upstream_code, request_id


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    """Bound provider-provided retry hints and use capped exponential fallback."""
    try:
        return max(0.0, min(float(retry_after or ""), 60.0))
    except (TypeError, ValueError):
        return min(2.0 ** attempt, 30.0)


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "".join(parts)
    raise ValueError("worker message content must contain text")


def _last_user_text(payload: object) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ValueError("worker request has no messages")
    for message in reversed(payload["messages"]):
        if isinstance(message, dict) and message.get("role") == "user":
            return remove_control_characters(_message_text(message.get("content")))
    raise ValueError("worker request has no user text")


def _messages_to_responses(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError("REQUEST_INVALID", "worker request has no messages", status=400)

    instructions: list[str] = []
    input_messages: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise GatewayError("REQUEST_INVALID", "worker message must be an object", status=400)
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            raise GatewayError("REQUEST_INVALID", "worker message role is unsupported", status=400)
        content = remove_control_characters(_message_text(message.get("content")))
        if role in {"system", "developer"}:
            instructions.append(content)
        else:
            input_messages.append({"role": role, "content": content})
    if not input_messages:
        raise GatewayError("REQUEST_INVALID", "worker request has no user text", status=400)

    result: dict[str, Any] = {"model": model, "input": input_messages, "store": False}
    if instructions:
        result["instructions"] = "\n\n".join(instructions)
    _copy_chat_options(payload, result)
    return result


def _copy_chat_options(payload: dict[str, Any], target: dict[str, Any]) -> None:
    if isinstance(payload.get("max_output_tokens"), int):
        target["max_output_tokens"] = payload["max_output_tokens"]
    elif isinstance(payload.get("max_tokens"), int):
        target["max_output_tokens"] = payload["max_tokens"]
    elif isinstance(payload.get("max_completion_tokens"), int):
        target["max_output_tokens"] = payload["max_completion_tokens"]
    if isinstance(payload.get("temperature"), (int, float)):
        target["temperature"] = payload["temperature"]
    reasoning_effort = payload.get("reasoning_effort")
    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
        target["reasoning"] = {"effort": reasoning_effort.strip()}
    response_format = payload.get("response_format")
    if response_format is not None:
        target["text"] = {"format": _responses_text_format(response_format)}


def _responses_text_format(response_format: object) -> dict[str, Any]:
    if not isinstance(response_format, dict):
        raise GatewayError("REQUEST_INVALID", "response_format must be an object", status=400)
    kind = response_format.get("type")
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        schema = response_format.get("json_schema")
        if not isinstance(schema, dict):
            schema = response_format
        name = schema.get("name")
        definition = schema.get("schema")
        if not isinstance(name, str) or not name.strip() or not isinstance(definition, dict):
            raise GatewayError("REQUEST_INVALID", "json_schema response_format is incomplete", status=400)
        return {
            "type": "json_schema",
            "name": name,
            "strict": bool(schema.get("strict", True)),
            "schema": definition,
        }
    raise GatewayError("REQUEST_INVALID", f"unsupported response_format type: {kind}", status=400)


def _responses_output_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise GatewayError("UPSTREAM_INVALID_RESPONSE", "Responses payload is not an object")
    parts: list[str] = []
    for message in payload.get("output", []):
        if not isinstance(message, dict):
            continue
        for content in message.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    if not parts:
        raise GatewayError("UPSTREAM_INVALID_RESPONSE", "Responses payload has no output_text")
    return "".join(parts)


def _chat_response_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise GatewayError("UPSTREAM_INVALID_RESPONSE", "Chat payload is not an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise GatewayError("UPSTREAM_INVALID_RESPONSE", "Chat payload has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise GatewayError("UPSTREAM_INVALID_RESPONSE", "Chat choice has no text content")
    return message["content"]


def _normalise_answer(answer: str, *, source_text: str, response_format: object) -> str:
    answer = _strip_json_wrappers(
        restore_immutable_identifiers(
            source_text,
            normalize_unicode_dashes(remove_control_characters(answer)),
        ).strip()
    )
    if response_format is None:
        if not answer:
            raise GatewayError("UPSTREAM_EMPTY_RESPONSE", "upstream translation is empty")
        return answer
    try:
        parsed = json.loads(answer)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GatewayError(
            "STRUCTURED_OUTPUT_INVALID",
            "provider did not return valid JSON for the BabelDOC batch",
        ) from exc
    parsed = _sanitize_json(parsed)
    # Restore protected identifiers inside JSON string values, not in the raw
    # JSON syntax.  This also keeps output valid when an identifier contains a
    # quote-like character in a future provider response.
    parsed = _restore_identifiers_in_json(parsed, source_text)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _structured_answer_with_retry(
    request_body: dict[str, Any],
    call: Any,
    *,
    first_response: dict[str, Any] | None = None,
    max_depth: int = 8,
) -> str:
    """Translate a BabelDOC JSON batch and split only on mapping failure.

    BabelDOC 0.6.x sends a final prompt containing a JSON array of paragraph
    items.  A provider can legally return valid JSON yet omit items when the
    completion is truncated.  Passing that response through makes BabelDOC
    fall back to the original paragraph (including any parser control bytes).
    We therefore validate IDs at the gateway and recursively bisect the same
    semantic items only when the mapping is incomplete.
    """
    request_body = _with_name_constraints(request_body)
    response_format = request_body.get("response_format")
    prompt = _last_user_text(request_body)
    contract = _extract_babeldoc_batch(prompt)

    def translate_once(body: dict[str, Any], raw_response: dict[str, Any] | None = None) -> str:
        if raw_response is None:
            raw = call(body)
        else:
            raw = _chat_response_content(raw_response)
        return _normalise_answer(
            raw,
            source_text=_last_user_text(body),
            response_format=response_format,
        )

    if contract is None:
        answer = translate_once(request_body, first_response)
        source_language, target_language = _request_languages(request_body)
        return auto_correct_translation(
            _last_user_text(request_body), answer, source_language, target_language
        )

    prefix, items = contract
    return _structured_batch_retry(
        request_body,
        prefix=prefix,
        items=items,
        call=call,
        response_format=response_format,
        first_response=first_response,
        depth=0,
        max_depth=max_depth,
    )


def _structured_batch_retry(
    request_body: dict[str, Any],
    *,
    prefix: str,
    items: list[dict[str, Any]],
    call: Any,
    response_format: object,
    first_response: dict[str, Any] | None,
    depth: int,
    max_depth: int,
    rule_correction: bool = False,
) -> str:
    body = _replace_last_user_message(
        request_body,
        prefix + json.dumps(items, ensure_ascii=False, indent=2),
    )
    if first_response is None:
        raw = call(body)
    else:
        raw = _chat_response_content(first_response)
    normalised = _normalise_answer(
        raw,
        source_text=_last_user_text(body),
        response_format=response_format,
    )
    parsed = _repair_structured_boundaries(_parse_structured_items(normalised), items)
    expected_ids = [str(item["id"]) for item in items]
    if _structured_items_match(parsed, expected_ids, expected_items=items):
        source_language, target_language = _request_languages(request_body)
        by_id = {str(item["id"]): item for item in parsed}
        errors = []
        for item in items:
            source = item.get("input", item.get("source", ""))
            row = by_id[str(item["id"])]
            output = row.get("output", row.get("translation", row.get("input", "")))
            # Check prose, not numeric/style attributes in BabelDOC's markup.
            source = _TAG_TOKEN_RE.sub("", source)
            output = _TAG_TOKEN_RE.sub("", output)
            output = auto_correct_translation(source, output, source_language, target_language)
            row["output"] = output
            defects = validate_name_retention(source, output, source_language, target_language)
            defects.extend(validate_translation_residue(source, output, source_language, target_language))
            defects.extend(validate_placeholders(source, output, rule_protected_tokens(source)))
            errors.extend(f"{item['id']}: {defect}" for defect in defects)
        if errors:
            if rule_correction:
                raise GatewayError("STRUCTURED_OUTPUT_VALIDATION_FAILED", "; ".join(errors))
            corrected = dict(request_body)
            corrected["messages"] = [*request_body["messages"], {
                "role": "system", "content": "AUTOMATIC CORRECTION: retain original English names and protected literals in their own items. " + "; ".join(errors),
            }]
            return _structured_batch_retry(
                corrected, prefix=prefix, items=items, call=call,
                response_format=response_format, first_response=None,
                depth=depth, max_depth=max_depth, rule_correction=True,
            )
        return json.dumps(
            _order_structured_items(parsed, expected_ids),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    if rule_correction or len(items) <= 1 or depth >= max_depth:
        raise GatewayError(
            "STRUCTURED_OUTPUT_MAPPING_INVALID",
            "provider response did not contain every BabelDOC batch item",
        )

    midpoint = len(items) // 2
    left = _structured_batch_retry(
        request_body,
        prefix=prefix,
        items=items[:midpoint],
        call=call,
        response_format=response_format,
        first_response=None,
        depth=depth + 1,
        max_depth=max_depth,
    )
    right = _structured_batch_retry(
        request_body,
        prefix=prefix,
        items=items[midpoint:],
        call=call,
        response_format=response_format,
        first_response=None,
        depth=depth + 1,
        max_depth=max_depth,
    )
    combined = _parse_structured_items(left) + _parse_structured_items(right)
    if not _structured_items_match(combined, expected_ids, expected_items=items):
        raise GatewayError(
            "STRUCTURED_OUTPUT_MAPPING_INVALID",
            "provider response could not be reassembled by stable IDs",
        )
    return json.dumps(
        _order_structured_items(combined, expected_ids),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _extract_babeldoc_batch(prompt: str) -> tuple[str, list[dict[str, Any]]] | None:
    marker = "## Here is the input:"
    marker_position = prompt.rfind(marker)
    if marker_position < 0:
        return None
    prefix = prompt[: marker_position + len(marker)].rstrip() + "\n\n"
    tail = prompt[marker_position + len(marker) :].strip()
    try:
        parsed = json.loads(tail)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not parsed or any(
        not isinstance(item, dict) or "id" not in item for item in parsed
    ):
        return None
    return prefix, parsed


def _request_languages(request_body: dict[str, Any]) -> tuple[str, str]:
    for message in request_body.get("messages", []):
        match = re.search(r"Translate from ([\w-]+) to ([\w-]+)", _message_text(message.get("content", "")), re.I)
        if match:
            return match.group(1), match.group(2)
    return "auto", "zh"


def _with_name_constraints(request_body: dict[str, Any]) -> dict[str, Any]:
    contract = _extract_babeldoc_batch(_last_user_text(request_body))
    if contract is None:
        return request_body
    prefix, items = contract
    source_language, target_language = _request_languages(request_body)
    constrained = []
    for item in items:
        source = item.get("input", item.get("source", ""))
        names = source_name_constraints(_TAG_TOKEN_RE.sub("", source), source_language, target_language)
        constrained.append({**item, "required_names": names} if names else item)
    return _replace_last_user_message(request_body, prefix + json.dumps(constrained, ensure_ascii=False, indent=2))


def _replace_last_user_message(payload: dict[str, Any], content: str) -> dict[str, Any]:
    copied = dict(payload)
    messages = [dict(message) for message in payload.get("messages", [])]
    for message in reversed(messages):
        if message.get("role") == "user":
            message["content"] = content
            break
    else:
        raise GatewayError("REQUEST_INVALID", "worker request has no user text", status=400)
    copied["messages"] = messages
    return copied


def _parse_structured_items(text: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GatewayError(
            "STRUCTURED_OUTPUT_INVALID",
            "provider did not return valid JSON for the BabelDOC batch",
        ) from exc
    if isinstance(parsed, dict) and ("output" in parsed or "input" in parsed):
        parsed = [parsed]
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        return []
    return parsed


_TAG_TOKEN_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9:_-]*)\b[^>]*>")
_STYLE_SEGMENT_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)


def _preserves_structural_boundaries(source: str, output: str) -> bool:
    """Check structural tag identity without constraining prose wording."""

    source_tags = [tag.casefold() for tag in _TAG_TOKEN_RE.findall(source)]
    output_tags = [tag.casefold() for tag in _TAG_TOKEN_RE.findall(output)]
    if source_tags != output_tags:
        return False
    source_segments = _STYLE_SEGMENT_RE.findall(source)
    if not source_segments:
        return True
    return len(source_segments) == len(_STYLE_SEGMENT_RE.findall(output))


def _restore_structural_boundary_whitespace(source: str, output: str) -> str:
    """Restore spaces adjacent to style tags after a model trims them."""

    source_segments = _STYLE_SEGMENT_RE.findall(source)
    matches = list(_STYLE_SEGMENT_RE.finditer(output))
    if not source_segments or len(source_segments) != len(matches):
        return output
    pieces: list[str] = []
    cursor = 0
    for source_segment, match in zip(source_segments, matches, strict=True):
        output_segment = match.group(1)
        if source_segment[:1].isspace() and not output_segment[:1].isspace():
            output_segment = " " + output_segment
        if source_segment[-1:].isspace() and not output_segment[-1:].isspace():
            output_segment += " "
        pieces.append(output[cursor : match.start(1)])
        pieces.append(output_segment)
        cursor = match.end(1)
    pieces.append(output[cursor:])
    return "".join(pieces)


def _repair_structured_boundaries(
    items: list[dict[str, Any]],
    expected_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {
        str(item.get("id")): item
        for item in expected_items
        if isinstance(item, dict) and item.get("id") is not None
    }
    repaired: list[dict[str, Any]] = []
    for item in items:
        copied = dict(item)
        output_key = next((key for key in ("output", "translation") if isinstance(copied.get(key), str)), None)
        expected = by_id.get(str(copied.get("id")))
        if output_key and expected is not None:
            source = expected.get("input", expected.get("source", ""))
            if isinstance(source, str):
                copied[output_key] = _restore_structural_boundary_whitespace(source, copied[output_key])
        repaired.append(copied)
    return repaired


def _structured_items_match(
    items: list[dict[str, Any]],
    expected_ids: list[str],
    *,
    expected_items: list[dict[str, Any]] | None = None,
) -> bool:
    found: list[str] = []
    by_expected_id = {
        str(item.get("id")): item
        for item in (expected_items or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    for item in items:
        item_id = item.get("id")
        output = item.get("output", item.get("translation", item.get("input")))
        if item_id is None or not isinstance(output, str):
            return False
        found.append(str(item_id))
        expected = by_expected_id.get(str(item_id))
        if expected is not None:
            source = expected.get("input", expected.get("source", ""))
            if isinstance(source, str) and not _preserves_structural_boundaries(source, output):
                return False
    return len(found) == len(expected_ids) and set(found) == set(expected_ids)


def _order_structured_items(items: list[dict[str, Any]], expected_ids: list[str]) -> list[dict[str, Any]]:
    by_id = {str(item["id"]): item for item in items}
    return [by_id[item_id] for item_id in expected_ids]


def _strip_json_wrappers(text: str) -> str:
    """Accept wrappers BabelDOC itself knows how to remove before JSON parse."""
    value = text.strip()
    if value.startswith("<json>"):
        value = value[6:]
    if value.endswith("</json>"):
        value = value[:-7]
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    if value.endswith("```"):
        value = value[:-3]
    return value.strip()


def _restore_identifiers_in_json(value: object, source_text: str) -> object:
    if isinstance(value, str):
        return restore_immutable_identifiers(source_text, value)
    if isinstance(value, list):
        return [_restore_identifiers_in_json(item, source_text) for item in value]
    if isinstance(value, dict):
        return {key: _restore_identifiers_in_json(item, source_text) for key, item in value.items()}
    return value


def _sanitize_json(value: object) -> Any:
    if isinstance(value, str):
        return normalize_unicode_dashes(remove_control_characters(value))
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_json(item) for key, item in value.items()}
    return value


def _replace_chat_response_content(payload: dict[str, Any], content: str) -> dict[str, Any]:
    copied = dict(payload)
    choices = list(copied.get("choices", []))
    if not choices or not isinstance(choices[0], dict):
        raise GatewayError("UPSTREAM_INVALID_RESPONSE", "Chat payload has no choices")
    first = dict(choices[0])
    message = dict(first.get("message", {}))
    message["content"] = content
    first["message"] = message
    choices[0] = first
    copied["choices"] = choices
    return copied


def _chat_completion(model: str, text: str) -> dict[str, object]:
    return {
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
