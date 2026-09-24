from __future__ import annotations

import json
from pathlib import Path

import pytest

import document_translator.services.babeldoc_pdf as module
from document_translator.services.babeldoc_pdf import (
    _normalise_table_response,
    _translate_and_patch_tables,
)
from document_translator.services.pdf_pipeline import inspect_pdf, repair_pdf_text_cmaps, validate_candidate


def _make_table_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=360, height=220)
    for y in (30, 62, 105, 150):
        page.draw_line((20, y), (320, y))
    page.draw_line((20, 30), (20, 150))
    page.draw_line((320, 30), (320, 150))
    page.draw_line((170, 30), (170, 150))
    page.insert_text((25, 50), "Name", fontsize=10)
    page.insert_text((25, 82), "Multi\nline", fontsize=10)
    page.insert_text((175, 82), "Source", fontsize=10)
    document.save(path)
    document.close()


class _Response:
    def __init__(self, payload: object):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def test_table_response_requires_explicit_output_and_rejects_duplicates() -> None:
    assert _normalise_table_response([{"id": "a", "output": "第一行\\n第二行"}]) == {"a": "第一行\n第二行"}
    with pytest.raises(RuntimeError, match="no output"):
        _normalise_table_response([{"id": "a", "input": "原文"}])
    with pytest.raises(RuntimeError, match="duplicate"):
        _normalise_table_response([
            {"id": "a", "output": "一"},
            {"id": "a", "output": "二"},
        ])


def test_table_route_batches_cells_once_and_validates_final_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    output = tmp_path / "table-patched.pdf"
    _make_table_pdf(source)
    _make_table_pdf(candidate)
    calls: list[list[dict[str, str]]] = []

    def fake_urlopen(request, *, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        items = json.loads(payload["messages"][-1]["content"].split("## Here is the input:", 1)[1])
        calls.append(items)
        result = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            [{"id": item["id"], "output": f"Translated {item['id']}"} for item in items],
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        return _Response(result)

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    run = _translate_and_patch_tables(
        source=source,
        candidate=candidate,
        destination=output,
        table_pages=(1,),
        source_language="en",
        target_language="zh-CN",
        model="gpt-5.6-terra",
        gateway_url="http://127.0.0.1:9999/v1",
        minimum_font_size=6,
    )

    assert len(calls) == 2
    assert [len(batch) for batch in calls] == [1, 2]
    assert run["status"] == "patched"
    assert output.is_file()
    repaired = output.with_name("repaired.pdf")
    repaired.write_bytes(output.read_bytes())
    repair_pdf_text_cmaps(repaired)
    validation = validate_candidate(source, repaired, inspect_pdf(source), target_language="zh-CN")
    assert validation["control_characters"] == []
    assert validation["minimum_observed_font_size"] >= 6
