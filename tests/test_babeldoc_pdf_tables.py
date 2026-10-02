from __future__ import annotations

import json
from pathlib import Path

import pytest

import document_translator.services.babeldoc_pdf as module
from document_translator.services.babeldoc_pdf import (
    _expand_long_table_cells,
    _normalise_table_response,
    _translate_and_patch_tables,
)
from document_translator.services.pdf_table import PdfTableCell
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


@pytest.mark.parametrize("branch", ["table", "rotated"])
@pytest.mark.parametrize("retain_name", [True, False])
def test_pdf_branches_validate_names_even_with_external_gateway(monkeypatch, branch, retain_name):
    calls = []
    def fake_urlopen(request, *, timeout):
        payload = json.loads(request.data)
        calls.append(payload)
        rows = json.loads(payload["messages"][1]["content"].split("## Here is the input:")[1])
        assert rows[0]["required_names"] == ["RAVI Rd."]
        output = "拉维路（RAVI Rd.）" if retain_name else "拉维路"
        return _Response({"choices": [{"message": {"content": json.dumps([{"id": "a", "output": output}])}}]})
    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    method = module._post_table_translation_batch if branch == "table" else module._post_rotated_translation_batch
    kwargs = dict(gateway_url="http://local.invalid/v1", model="fake", source_language="en", target_language="zh-CN", items=[{"id": "a", "input": "RAVI Rd."}])
    if retain_name:
        method(**kwargs)
        assert len(calls) == 1
    else:
        with pytest.raises(RuntimeError, match="PROPER_NAME_MISSING"):
            method(**kwargs)
        assert len(calls) == (1 if branch == "table" else 2)


def test_table_response_requires_explicit_output_and_rejects_duplicates() -> None:
    assert _normalise_table_response([{"id": "a", "output": "第一行\\n第二行"}]) == {"a": "第一行\n第二行"}
    assert _normalise_table_response([{"id": "a", "output": "IG‑541\x00"}]) == {"a": "IG-541"}
    with pytest.raises(RuntimeError, match="no output"):
        _normalise_table_response([{"id": "a", "input": "原文"}])
    with pytest.raises(RuntimeError, match="duplicate"):
        _normalise_table_response([
            {"id": "a", "output": "一"},
            {"id": "a", "output": "二"},
        ])


def test_long_cell_parts_are_sent_in_separate_bounded_requests() -> None:
    cell = PdfTableCell(
        id="cell-1",
        page_number=1,
        table_number=1,
        row=1,
        column=1,
        rect=(0, 0, 100, 100),
        text="word " * 1000,
    )
    groups, parts = _expand_long_table_cells(
        (cell,),
        4500,
        maximum_text_chars=3000,
    )

    assert len(parts["cell-1"]) == 2
    assert [len(group) for group in groups] == [1, 1]
    assert all(sum(len(item["input"]) + 96 for item in group) <= 4500 for group in groups)


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

    assert len(calls) == 1
    assert [len(batch) for batch in calls] == [3]
    assert run["status"] == "patched"
    assert output.is_file()
    repaired = output.with_name("repaired.pdf")
    repaired.write_bytes(output.read_bytes())
    repair_pdf_text_cmaps(repaired)
    validation = validate_candidate(source, repaired, inspect_pdf(source), target_language="zh-CN")
    assert validation["control_characters"] == []
    assert validation["minimum_observed_font_size"] >= 6
