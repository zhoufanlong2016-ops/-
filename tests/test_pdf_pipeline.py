from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from pathlib import Path
from urllib.error import HTTPError

import pytest

import document_translator.__main__ as cli
from document_translator.services.babeldoc_pdf import BabelDocPdfTranslationService
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


def make_pdf(path, text: str = "Hello PDF", *, fontsize: float = 11):
    import fitz

    document = fitz.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 50), text, fontsize=fontsize)
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


def test_candidate_validation_rejects_control_characters_and_target_language_residue(tmp_path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source)
    make_pdf(candidate, "中文\u0003")

    with pytest.raises(PdfPreflightError, match="control characters"):
        validate_candidate(source, candidate, inspect_pdf(source), target_language="en")


def test_candidate_validation_rejects_changed_identifiers_and_unicode_dashes(tmp_path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source, "Document ID: TEST-PDF-001")
    make_pdf(candidate, "文档编号：TEST‑PDF‑001")

    with pytest.raises(PdfPreflightError, match="non-ASCII dash|immutable identifiers"):
        validate_candidate(source, candidate, inspect_pdf(source), target_language="zh")


def test_candidate_validation_rejects_unreadable_font_scaling(tmp_path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    make_pdf(source)
    make_pdf(candidate, "tiny text", fontsize=3)

    with pytest.raises(PdfPreflightError, match="minimum font size"):
        validate_candidate(
            source,
            candidate,
            inspect_pdf(source),
            target_language="zh",
            minimum_font_size=6,
        )


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


def test_table_like_pdf_is_classified_as_complex_and_rejected_before_worker(tmp_path):
    source = tmp_path / "table.pdf"
    destination = tmp_path / "translated.pdf"
    report = tmp_path / "preflight.json"
    make_table_pdf(source)

    preflight = inspect_pdf(source)
    assert preflight.classification == "B"
    assert preflight.table_pages == (1,)
    assert preflight.visual_review_required is True

    with pytest.raises(PdfPreflightError, match="class B.*allow-complex-pdf"):
        BabelDocPdfTranslationService(executable="not-called").translate_file(
            source,
            destination,
            source_language="zh-CN",
            target_language="en",
            provider="gpt",
            model="gpt-5.6-terra",
            report_path=report,
        )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "REJECTED_PREFLIGHT"
    assert payload["preflight"]["table_pages"] == [1]
    assert not destination.exists()


def test_scanned_like_pdf_is_rejected_before_worker_starts(tmp_path):
    import fitz

    source = tmp_path / "image-only.pdf"
    document = fitz.open()
    document.new_page()
    document.save(source)
    document.close()

    with pytest.raises(PdfPreflightError, match="class D"):
        BabelDocPdfTranslationService(executable="not-called").translate_file(
            source, tmp_path / "out.pdf", source_language="en", target_language="zh", provider="qwen", model="qwen-plus",
        )


def test_pdf_destination_is_never_silently_overwritten(tmp_path):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "out.pdf"
    make_pdf(source)
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="already exists"):
        BabelDocPdfTranslationService(executable="not-called").translate_file(
            source, destination, source_language="en", target_language="zh", provider="qwen", model="qwen-plus",
        )


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
    with pytest.raises(ValueError, match="qwen or gpt"):
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


def test_default_pdf_worker_resolves_from_the_active_project_environment():
    service = BabelDocPdfTranslationService()
    assert service.executable.casefold().endswith(".venv\\scripts\\pdf2zh_next.exe")


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


def test_gateway_splits_only_an_incomplete_babeldoc_batch_and_reassembles_ids():
    items = [{"id": index, "input": f"paragraph {index}"} for index in range(4)]
    prompt = "## Here is the input:\n" + json.dumps(items, ensure_ascii=False)
    request = {
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    calls: list[int] = []

    def fake_provider(body):
        content = body["messages"][-1]["content"]
        current = json.loads(content.split("## Here is the input:", 1)[1])
        calls.append(len(current))
        # Simulate a valid-but-truncated provider response for every batch
        # larger than one item; the gateway must then bisect it.
        returned = current if len(current) == 1 else current[:1]
        return json.dumps([
            {"id": row["id"], "output": f"译文 {row['id']}"} for row in returned
        ], ensure_ascii=False)

    result = _structured_answer_with_retry(request, fake_provider)

    parsed = json.loads(result)
    assert [row["id"] for row in parsed] == [0, 1, 2, 3]
    assert calls == [4, 2, 1, 1, 2, 1, 1]


def test_high_level_pdf_success_is_published_atomically(tmp_path, monkeypatch):
    import document_translator.services.babeldoc_pdf as module

    source = tmp_path / "source.pdf"
    candidate = tmp_path / "worker-candidate.pdf"
    destination = tmp_path / "translated.pdf"
    report = tmp_path / "translated.json"
    make_pdf(source, "Hello PDF")
    make_pdf(candidate, "Translated PDF")

    monkeypatch.setattr(
        module,
        "_run_high_level_translation",
        lambda **_kwargs: (SimpleNamespace(mono_pdf_path=candidate), {"progress_events": 3}),
    )

    output, _preflight, report_path = module.BabelDocPdfTranslationService().translate_file(
        source,
        destination,
        source_language="en",
        target_language="zh",
        provider="gpt",
        model="gpt-5.6-terra",
        gateway_base_url="https://example.test/v1",
        report_path=report,
    )

    assert output == destination.resolve()
    assert destination.is_file()
    assert candidate.is_file()  # worker-owned candidate remains outside the temp run dir
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ACCEPTED"
    assert payload["run"]["engine"] == "babeldoc.format.pdf.high_level.async_translate"


def test_pdf_failure_quarantines_candidate_and_writes_report(tmp_path, monkeypatch):
    import document_translator.services.babeldoc_pdf as module

    source = tmp_path / "source.pdf"
    candidate = tmp_path / "worker-candidate.pdf"
    destination = tmp_path / "translated.pdf"
    report = tmp_path / "translated.json"
    make_pdf(source, "Hello PDF")
    make_pdf(candidate, "中文\x03")

    monkeypatch.setattr(
        module,
        "_run_high_level_translation",
        lambda **_kwargs: (SimpleNamespace(mono_pdf_path=candidate), {"progress_events": 2}),
    )

    with pytest.raises(PdfPreflightError, match="control characters"):
        module.BabelDocPdfTranslationService().translate_file(
            source,
            destination,
            source_language="en",
            target_language="zh",
            provider="gpt",
            model="gpt-5.6-terra",
            gateway_base_url="https://example.test/v1",
            report_path=report,
        )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "FAILED"
    quarantine = Path(payload["quarantine_dir"])
    assert quarantine.is_dir()
    assert Path(payload["quarantined_candidate"]).is_file()
    assert not destination.exists()
