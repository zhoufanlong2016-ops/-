import json

import pytest

from document_translator import __main__ as cli
from document_translator.adapters import dwg
from document_translator.core import TranslationResult, sha256_text
from document_translator.services.dwg_translation import DwgTranslationService, DwgTranslationServiceError


class FakeProvider:
    provider_name = "fake"
    prompt_version = "v1"
    glossary_version = "none"

    class config:
        model = "fake-model"

    def translate_unit(self, unit):
        translation = unit.source_text.replace("Hello", "你好")
        return TranslationResult(
            unit_id=unit.id, translation=translation, provider=self.provider_name, model=self.config.model,
            prompt_version=self.prompt_version, glossary_version=self.glossary_version,
            source_hash=sha256_text(unit.source_text), result_hash=sha256_text(translation),
            request_count=1, validation_status="valid",
        )

    def translate_batch(self, units):
        return [self.translate_unit(unit) for unit in units]


class BrokenMTextProvider(FakeProvider):
    def translate_unit(self, unit):
        result = super().translate_unit(unit)
        return result.model_copy(update={"translation": "missing tokens", "result_hash": sha256_text("missing tokens")})


def _metadata() -> dwg.TextMetadata:
    return dwg.TextMetadata(
        position=dwg.PointData(x=0.0, y=0.0, z=0.0), normal=dwg.PointData(x=0.0, y=0.0, z=1.0),
        rotation=0.0, width=0.0, height=2.5, width_factor=1.0, style_name="Standard", style_handle="A",
        color_index=256, color_method="ByLayer", block_handle=None, attribute_tag=None,
    )


def _write_export(tmp_path, *, mtext: bool = False):
    source = tmp_path / "source.dwg"
    source.write_bytes(b"DWG fixture")
    raw = r"{Hello\Pworld}" if mtext else "Hello"
    source_text, sequences = dwg.protect_mtext(raw) if mtext else (raw, ())
    item = dwg.TextItem(
        handle="A", entity_type="MText" if mtext else "DBText", space="Model", layer="TEXT",
        source_text=source_text, source_hash=dwg.sha256_text(raw), bounds=None, metadata=_metadata(),
        protected_sequences=sequences,
    )
    exported = dwg.ExportDocument(
        schema_version=1, operation="export_result", source_dwg=str(source), source_sha256=dwg.sha256_file(source),
        structure=dwg.StructuralSnapshot(entity_count=1, layer_count=1, block_count=1, layout_count=1, xref_count=0),
        items=(item,),
    )
    export_json = tmp_path / "export.json"
    export_json.write_text(exported.model_dump_json(), encoding="utf-8")
    return export_json, exported


def test_service_creates_import_task_and_gbk_script_without_writing_dwg(tmp_path) -> None:
    export_json, exported = _write_export(tmp_path, mtext=True)
    destination = tmp_path / "translated.dwg"
    task_json = tmp_path / "import.json"
    result_json = tmp_path / "result.json"
    script = tmp_path / "import.scr"

    outcome = DwgTranslationService(FakeProvider()).prepare_import(
        export_json, destination_dwg=destination, task_json=task_json, result_json=result_json, command_script=script,
        source_language="en", target_language="zh-CN",
    )

    assert len(outcome.units) == len(outcome.results) == 1
    assert outcome.units[0].format.value == "dwg"
    assert outcome.units[0].location.object_id == "A"
    assert outcome.import_task.destination_dwg == str(destination.resolve())
    assert not destination.exists()
    task = json.loads(task_json.read_text(encoding="utf-8"))
    assert task["translations"][0]["translated_text"].count("⟦MT_") == 3
    assert script.read_bytes().decode("gbk") == f'TR_IMPORT\r\n"{task_json.resolve()}"\r\n'
    assert dwg.sha256_file(exported.source_dwg) == exported.source_sha256


def test_service_carries_explicit_font_policy_to_import_task(tmp_path) -> None:
    export_json, _ = _write_export(tmp_path)
    outcome = DwgTranslationService(FakeProvider()).prepare_import(
        export_json, destination_dwg=tmp_path / "translated.dwg", task_json=tmp_path / "import.json",
        result_json=tmp_path / "result.json", command_script=tmp_path / "import.scr",
        source_language="en", target_language="zh-CN", font_policy={"Standard": "NotoSansSC-Regular.ttf"},
    )
    decision = outcome.import_task.translations[0].font_decision
    assert decision is not None
    assert decision.target_font_file == "NotoSansSC-Regular.ttf"


def test_service_carries_width_compensation_font_policy(tmp_path) -> None:
    export_json, _ = _write_export(tmp_path)
    outcome = DwgTranslationService(FakeProvider()).prepare_import(
        export_json, destination_dwg=tmp_path / "translated.dwg", task_json=tmp_path / "import.json",
        result_json=tmp_path / "result.json", command_script=tmp_path / "import.scr",
        source_language="en", target_language="zh-CN",
        font_policy={"Standard": {
            "target_font_file": "NotoSansSC-Regular.ttf",
            "width_factor": 0.79,
            "width_ratio": 1.27,
            "review_required": True,
            "review_reason": "overflow compensation",
        }},
    )
    decision = outcome.import_task.translations[0].font_decision
    assert decision is not None
    assert decision.width_factor == 0.79
    assert decision.review_required is True


def test_service_rejects_a_provider_that_drops_mtext_tokens(tmp_path) -> None:
    export_json, _ = _write_export(tmp_path, mtext=True)

    with pytest.raises(DwgTranslationServiceError, match="PLACEHOLDER_MISMATCH"):
        DwgTranslationService(BrokenMTextProvider(), max_attempts=1).prepare_import(
            export_json, destination_dwg=tmp_path / "translated.dwg", task_json=tmp_path / "import.json",
            result_json=tmp_path / "result.json", command_script=tmp_path / "import.scr",
            source_language="en", target_language="zh-CN",
        )


def test_cli_prepares_a_dwg_import_task_without_opening_autocad(tmp_path, monkeypatch, capsys) -> None:
    export_json, _ = _write_export(tmp_path)

    monkeypatch.setattr(cli, "_provider_for", lambda args, client, glossary: FakeProvider())
    assert cli.main([
        "prepare-dwg-import", str(export_json), str(tmp_path / "translated.dwg"),
        str(tmp_path / "import.json"), str(tmp_path / "result.json"), str(tmp_path / "import.scr"),
        "--source-language", "en", "--target-language", "zh-CN",
    ]) == 0

    assert (tmp_path / "import.json").is_file()
    assert (tmp_path / "import.scr").is_file()
    assert not (tmp_path / "translated.dwg").exists()
    assert "translated 1 DWG text items" in capsys.readouterr().out


def test_service_submits_dwg_items_in_bounded_batches(tmp_path) -> None:
    export_json, exported = _write_export(tmp_path)
    item = exported.items[0]
    repeated = exported.model_copy(update={"items": tuple(item.model_copy(update={"handle": f"{index + 10:X}"}) for index in range(17))})
    export_json.write_text(repeated.model_dump_json(), encoding="utf-8")

    class RecordingBatchProvider(FakeProvider):
        def __init__(self):
            self.batch_sizes = []

        def translate_batch(self, units):
            self.batch_sizes.append(len(units))
            return super().translate_batch(units)

    provider = RecordingBatchProvider()
    DwgTranslationService(provider).prepare_import(
        export_json, destination_dwg=tmp_path / "translated.dwg", task_json=tmp_path / "import.json",
        result_json=tmp_path / "result.json", command_script=tmp_path / "import.scr",
        source_language="en", target_language="zh-CN",
    )
    assert provider.batch_sizes == [8, 8, 1]


def test_service_retries_a_rate_limited_batch_with_backoff(tmp_path, monkeypatch) -> None:
    export_json, _ = _write_export(tmp_path)
    sleeps = []

    class RateLimitedThenReady(FakeProvider):
        def __init__(self):
            self.calls = 0

        def translate_batch(self, units):
            self.calls += 1
            if self.calls == 1:
                error = RuntimeError("limited")
                error.code = "HTTP_429"
                raise error
            return super().translate_batch(units)

    monkeypatch.setattr("document_translator.services.dwg_translation.time.sleep", sleeps.append)
    DwgTranslationService(RateLimitedThenReady()).prepare_import(
        export_json, destination_dwg=tmp_path / "translated.dwg", task_json=tmp_path / "import.json",
        result_json=tmp_path / "result.json", command_script=tmp_path / "import.scr",
        source_language="en", target_language="zh-CN",
    )
    assert sleeps == [1]
