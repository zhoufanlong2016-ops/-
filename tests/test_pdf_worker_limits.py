from document_translator.pdf_worker import _pdf_runtime_limits


def test_pdf_runtime_limits_use_bounded_parallel_default(monkeypatch):
    monkeypatch.delenv("DOCUMENT_TRANSLATOR_PDF_QPS", raising=False)
    monkeypatch.delenv("DOCUMENT_TRANSLATOR_PDF_WORKERS", raising=False)
    assert _pdf_runtime_limits() == (6, 6)


def test_pdf_runtime_limits_allow_safe_downshift(monkeypatch):
    monkeypatch.setenv("DOCUMENT_TRANSLATOR_PDF_QPS", "1")
    monkeypatch.setenv("DOCUMENT_TRANSLATOR_PDF_WORKERS", "8")
    assert _pdf_runtime_limits() == (1, 1)
