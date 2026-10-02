from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

import document_translator.__main__ as cli
from document_translator.services.pdf_pipeline import (
    PdfPreflightError,
    inspect_pdf,
    remove_control_characters,
    normalize_unicode_dashes,
    publish_candidate,
    repair_pdf_text_cmaps,
    restore_immutable_identifiers,
    sha256_file,
    extract_mixed_immutable_identifiers,
    validate_candidate,
)
from document_translator.services.translation_gateway import (
    GatewayConfig,
    _chat_completion,
    _last_user_text,
    _messages_to_responses,
    _normalise_answer,
    _responses_output_text,
    _structured_answer_with_retry,
)


def make_pdf(path, text: str = "Hello PDF", *, fontsize: float = 11, fontname: str = "helv"):
    import fitz

    document = fitz.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 50), text, fontsize=fontsize, fontname=fontname)
    document.save(path)
    document.close()


def make_table_pdf(path):
    import fitz

    document = fitz.open()
    page = document.new_page(width=300, height=400)
    for y in (80, 110, 150, 190, 230):
        page.draw_line((30, y), (270, y))
    for x in (30, 120, 270):
        page.draw_line((x, 80), (x, 230))
    page.insert_text((40, 100), "Header")
    page.insert_text((40, 140), "Cell")
    document.save(path)
    document.close()


def test_native_pdf_preflight_and_candidate_validation(tmp_path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source)
    make_pdf(candidate, "Translated PDF")

    preflight = inspect_pdf(source)
    validation = validate_candidate(source, candidate, preflight, target_language="zh")

    assert preflight.classification == "A"
    assert preflight.page_count == 1
    assert len(validation["candidate_hash"]) == 64


@pytest.mark.parametrize("target_language", ["zh", "zh-CN", "Chinese"])
def test_candidate_validation_records_missing_original_name_as_warning(tmp_path, target_language):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source, "RAVI Rd.", fontsize=8)
    make_pdf(candidate, "拉维路", fontsize=4, fontname="china-s")
    preflight = inspect_pdf(source)

    result = validate_candidate(source, candidate, preflight, target_language=target_language)
    assert result["name_warnings"]
    assert "RAVI Rd" in result["name_warnings"][0]
    assert sha256_file(source) == preflight.source_hash


@pytest.mark.parametrize("source_text", ["RAVI Rd.\nRAVI Rd.", "RAVI Rd. and RAVI Rd."])
def test_candidate_validation_preserves_name_occurrence_counts(tmp_path, source_text):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source, source_text)
    make_pdf(candidate, "RAVI Rd.")

    result = validate_candidate(source, candidate, inspect_pdf(source), target_language="zh")
    assert any("expected 2, got 1" in warning for warning in result["name_warnings"])


@pytest.mark.parametrize(
    "source_text,candidate_text,target_language",
    [
        ("RAVI Rd.", "\nRAVI Rd.", "zh"),
        ("RAVI Rd.\nRAVI Rd.", "RAVI Rd. and RAVI Rd.", "zh"),
        ("RAVI\nRd.", "Translated text", "zh"),
        ("RAVI Rd.", "Translated text", "en"),
        ("RAVI Rd.", "Translated text", ""),
    ],
)
def test_candidate_validation_allows_retained_names_and_other_targets(
    tmp_path, source_text, candidate_text, target_language
):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source, source_text)
    make_pdf(candidate, candidate_text)
    preflight = inspect_pdf(source)

    result = validate_candidate(source, candidate, preflight, target_language=target_language)

    assert len(result["candidate_hash"]) == 64
    assert sha256_file(source) == preflight.source_hash


@pytest.mark.parametrize("source_size,candidate_size,accepted", [(8, 4, True), (12, 6, True), (20, 9, False)])
def test_font_acceptance_uses_corresponding_source_ratio(tmp_path, source_size, candidate_size, accepted):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source, "Original text", fontsize=source_size)
    make_pdf(candidate, "Translated text", fontsize=candidate_size)
    result = validate_candidate(source, candidate, inspect_pdf(source), target_language="zh")
    if accepted:
        assert result["font_size_ratio_checks"][0]["ratio"] >= 0.5
        assert not any("font size" in w for w in result["candidate_warnings"])
    else:
        assert result["font_size_ratio_checks"][0]["ratio"] < 0.5
        assert any("font size" in w for w in result["candidate_warnings"])

def test_candidate_validation_flags_control_characters_and_target_language_residue(tmp_path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source)
    make_pdf(candidate, "中文\u0003")

    result = validate_candidate(source, candidate, inspect_pdf(source), target_language="en")
    assert any("control characters" in w for w in result["candidate_warnings"])


def test_candidate_validation_flags_changed_identifiers_and_unicode_dashes(tmp_path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source, "Document ID: TEST-PDF-001")
    make_pdf(candidate, "文档编号：TEST‑PDF‑001")

    result = validate_candidate(source, candidate, inspect_pdf(source), target_language="zh")
    assert any("non-ASCII dash" in w or "immutable identifiers" in w for w in result["candidate_warnings"])


def test_candidate_validation_flags_unreadable_font_scaling_for_review(tmp_path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source)
    make_pdf(candidate, "tiny text", fontsize=3)

    result = validate_candidate(
        source,
        candidate,
        inspect_pdf(source),
        target_language="zh",
        minimum_font_size=6,
    )
    assert any("font size" in w for w in result["candidate_warnings"])


def test_mixed_document_reference_extraction_normalises_pdf_line_wrapping() -> None:
    assert extract_mixed_immutable_identifiers("文号：福设院〔2024〕\n54 号") == ("福设院〔2024〕54号",)


def test_model_dash_variants_are_normalised_to_ascii_hyphens() -> None:
    assert normalize_unicode_dashes("A–B—C‑D") == "A-B-C-D"




def test_cmap_repair_is_noop_when_no_synthetic_space_is_present(tmp_path):
    candidate = tmp_path / "candidate.pdf"
    make_pdf(candidate, "ordinary text")
    before = sha256_file(candidate)

    assert repair_pdf_text_cmaps(candidate) == {"repaired_fonts": 0, "repairs": []}
    assert sha256_file(candidate) == before


def test_table_like_pdf_is_classified_as_complex_before_worker(tmp_path):
    source = tmp_path / "table.pdf"
    destination = tmp_path / "translated.pdf"
    make_table_pdf(source)

    preflight = inspect_pdf(source)
    assert preflight.classification == "B"
    assert preflight.table_pages == (1,)
    assert preflight.visual_review_required is True

    assert destination is not source


def test_scanned_like_pdf_is_classified_for_mineru_ocr(tmp_path):
    import fitz

    source = tmp_path / "image-only.pdf"
    document = fitz.open()
    document.new_page()
    document.save(source)
    document.close()

    assert inspect_pdf(source).classification == "D"


def test_pdf_destination_is_never_silently_overwritten(tmp_path):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "out.pdf"
    make_pdf(source)
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="already exists"):
        publish_candidate(source, destination)


def test_atomic_publish_never_replaces_a_destination_created_concurrently(tmp_path):
    candidate = tmp_path / "candidate.pdf"
    destination = tmp_path / "out.pdf"
    candidate.write_bytes(b"new")
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="already exists"):
        publish_candidate(candidate, destination)
    assert candidate.read_bytes() == b"new"
    assert destination.read_bytes() == b"existing"


def test_provider_routing_is_explicit_and_never_inferred_from_key_environment(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen")
    monkeypatch.setenv("OPENAI_API_KEY", "gpt")

    qwen = GatewayConfig.from_provider(provider="qwen", model="qwen-plus")
    gpt = GatewayConfig.from_provider(provider="gpt", model="gpt-5.4")

    assert qwen.credential_env == "DASHSCOPE_API_KEY"
    assert gpt.credential_env == "OPENAI_API_KEY"
    with pytest.raises(ValueError, match="qwen, gpt or deepseek"):
        GatewayConfig.from_provider(provider="openai", model="gpt-5.4")


def test_pdf_cli_uses_only_explicit_qwen_or_gpt_and_requires_a_model():
    parser = cli._parser()
    args = parser.parse_args([
        "translate-pdf", "source.pdf", "out.pdf", "--source-language", "en",
        "--target-language", "zh", "--provider", "gpt", "--model", "gpt-5.4",
    ])
    assert (args.provider, args.model) == ("gpt", "gpt-5.4")
    with pytest.raises(SystemExit):
        parser.parse_args(["translate-pdf", "source.pdf", "out.pdf", "--source-language", "en", "--target-language", "zh"])


def test_gateway_responses_adapter_uses_only_user_text_and_returns_chat_contract():
    assert _last_user_text({"messages": [{"role": "system", "content": "policy"}, {"role": "user", "content": "translate me"}]}) == "translate me"
    response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "译文"}]}]}
    assert _responses_output_text(response) == "译文"
    assert _chat_completion("gpt-5.4", "译文")["choices"][0]["message"]["content"] == "译文"


def test_gateway_retries_transient_upstream_failure_with_bounded_retry_after(monkeypatch):
    import document_translator.services.translation_gateway as module

    attempts: list[float] = []
    sleeps: list[float] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(_request, *, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise HTTPError(
                "https://example.test",
                429,
                "rate limited",
                {"Retry-After": "2"},
                BytesIO(b"ignored"),
            )
        return Response()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    assert module._request_json("https://example.test", "secret", {"x": 1}) == {"ok": True}
    assert attempts == [module._UPSTREAM_TIMEOUT_SECONDS, module._UPSTREAM_TIMEOUT_SECONDS]
    assert sleeps == [2.0]


def test_gateway_responses_adapter_preserves_roles_options_and_removes_c0():
    request = {
        "messages": [
            {"role": "system", "content": "policy\x03"},
            {"role": "user", "content": "translate\x04 me"},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 512,
        "temperature": 0,
    }

    mapped = _messages_to_responses(request, model="gpt-5.6-terra")

    assert mapped["model"] == "gpt-5.6-terra"
    assert mapped["store"] is False
    assert mapped["instructions"] == "policy"
    assert mapped["input"] == [{"role": "user", "content": "translate me"}]
    assert mapped["max_output_tokens"] == 512
    assert mapped["text"]["format"] == {"type": "json_object"}


def test_gateway_normalises_structured_output_and_restores_identifier_spelling():
    source = "Document ID: TEST-PDF-001"
    answer = '[{"id":"TEST‑PDF‑001","output":"译文\\u0003"}]'

    normalised = _normalise_answer(
        answer,
        source_text=source,
        response_format={"type": "json_object"},
    )

    parsed = json.loads(normalised)
    assert parsed[0]["id"] == "TEST-PDF-001"
    assert "\x03" not in normalised
    assert restore_immutable_identifiers(source, "TEST‑PDF‑001") == "TEST-PDF-001"
    assert remove_control_characters("a\x03b\r\n\t") == "ab\r\n\t"


def test_pdf_qwen_route_rejects_qwen_mt_models_before_worker_starts():
    with pytest.raises(ValueError, match="general Qwen model"):
        GatewayConfig.from_provider(provider="qwen", model="qwen-mt-plus")


def test_gateway_retries_an_incomplete_structured_batch_without_bisection():
    items = [{"id": index, "input": f"paragraph {index}"} for index in range(4)]
    prompt = "## Here is the input:\n" + json.dumps(items, ensure_ascii=False)
    request = {
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    calls: list[int] = []

    def fake_provider(body):
        content = next(message["content"] for message in body["messages"] if message["role"] == "user")
        current = json.loads(content.split("## Here is the input:", 1)[1])
        calls.append(len(current))
        # Simulate a valid-but-truncated provider response.  The gateway must
        # retry the same semantic batch, not bisect it.
        returned = current if len(current) == 1 else current[:1]
        return json.dumps([
            {"id": row["id"], "output": f"译文 {row['id']}"} for row in returned
        ], ensure_ascii=False)

    with pytest.raises(RuntimeError, match="every structured batch item"):
        _structured_answer_with_retry(request, fake_provider)
    assert calls == [4, 4]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_gateway_names_use_one_batch_correction_without_splitting(repair_succeeds):
    items = [{"id": "road", "input": "RAVI Rd."}, {"id": "place", "input": "SHAREEF COLONY DS"}]
    request = {"messages": [{"role": "system", "content": "Translate from auto to zh-CN."},
                            {"role": "user", "content": "## Here is the input:\n" + json.dumps(items)}],
               "response_format": {"type": "json_object"}}
    calls = []
    validation_calls = []

    def provider(body):
        content = next(m["content"] for m in body["messages"] if m["role"] == "user")
        rows = json.loads(content.split("## Here is the input:")[1])
        calls.append(rows)
        if len(calls) == 2:
            correction = next(m["content"] for m in body["messages"] if m["role"] == "system" and "AUTOMATIC CORRECTION" in m["content"])
            assert "CANDIDATE OUTPUT TO CORRECT" in correction
            assert "PROPER_NAME_MISSING" in correction
            assert '"id":"road"' in correction
        return json.dumps([{"id": row["id"], "output": "译文" + ("（" + row["input"] + "）" if repair_succeeds and len(calls) == 2 else "")} for row in reversed(rows)])

    if repair_succeeds:
        result = json.loads(
            _structured_answer_with_retry(
                request,
                provider,
                audit_validation=lambda phase, errors: validation_calls.append((phase, errors)),
            )
        )
        assert [row["id"] for row in result] == ["road", "place"]
    else:
        with pytest.raises(RuntimeError, match="PROPER_NAME_MISSING"):
            _structured_answer_with_retry(
                request,
                provider,
                audit_validation=lambda phase, errors: validation_calls.append((phase, errors)),
            )
    assert [len(rows) for rows in calls] == [2, 2]
    assert calls[0][0]["required_names"] == ["RAVI Rd."]
    assert calls[0][1]["required_names"] == ["SHAREEF COLONY"]
    assert [phase for phase, _errors in validation_calls] == ["initial", "correction"]
    assert "PROPER_NAME_MISSING" in validation_calls[0][1][0]
    if repair_succeeds:
        assert validation_calls[1][1] == []
    else:
        assert "PROPER_NAME_MISSING" in validation_calls[1][1][0]

