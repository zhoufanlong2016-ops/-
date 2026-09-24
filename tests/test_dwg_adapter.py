import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from document_translator.adapters import dwg


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source.dwg"
    path.write_bytes(b"This is only a contract-test fixture, not a parsed DWG.")
    return path


def _metadata() -> dwg.TextMetadata:
    return dwg.TextMetadata(
        position=dwg.PointData(x=0.0, y=0.0, z=0.0),
        normal=dwg.PointData(x=0.0, y=0.0, z=1.0),
        rotation=0.0,
        width=0.0,
        height=2.5,
        width_factor=1.0,
        style_name="Standard",
        style_handle="A",
        color_index=256,
        color_method="ByLayer",
        block_handle=None,
        attribute_tag=None,
    )


def _item(handle: str = "A", entity_type: str = "DBText") -> dwg.TextItem:
    raw = "Hello"
    text = raw
    sequences: tuple[dwg.ProtectedSequence, ...] = ()
    if entity_type == "MText":
        text, sequences = dwg.protect_mtext(r"{Hello\Pworld}")
        raw = r"{Hello\Pworld}"
    return dwg.TextItem(
        handle=handle,
        entity_type=entity_type,
        space="Model",
        layer="TEXT",
        source_text=text,
        source_hash=dwg.sha256_text(raw),
        bounds=None,
        metadata=_metadata(),
        protected_sequences=sequences,
    )


def _export(source: Path, items: tuple[dwg.TextItem, ...]) -> dwg.ExportDocument:
    return dwg.ExportDocument(
        schema_version=1,
        operation="export_result",
        source_dwg=str(source),
        source_sha256=dwg.sha256_file(source),
        structure=dwg.StructuralSnapshot(entity_count=2, layer_count=1, block_count=2, layout_count=2, xref_count=0),
        items=items,
    )


def _write_export(path: Path, document: dwg.ExportDocument) -> None:
    path.write_text(json.dumps(document.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")


def test_strict_schema_version_operation_and_unknown_field_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path)
    base = {
        "schema_version": 1, "operation": "export", "source_dwg": str(source),
        "export_json": str(tmp_path / "export.json"), "layer_allow": (), "layer_deny": (), "overwrite": False,
    }
    assert dwg.ExportTask.model_validate(base, strict=True).operation == "export"
    for mutation in ({"schema_version": 2}, {"operation": "import"}, {"unexpected": True}):
        candidate = base | mutation
        with pytest.raises(ValidationError):
            dwg.ExportTask.model_validate(candidate, strict=True)


def test_handle_type_duplicate_and_source_hash_validation_are_strict(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with pytest.raises(ValidationError, match="handle"):
        _item("a")
    with pytest.raises(ValidationError):
        _item("A", "Text")
    item = _item()
    with pytest.raises(ValidationError, match="source_hash"):
        dwg.TextItem(**(item.model_dump() | {"source_hash": "0" * 64}))
    with pytest.raises(ValidationError, match="duplicate"):
        _export(source, (item, item))


def test_source_destination_missing_extension_and_overwrite_protection(tmp_path: Path) -> None:
    source = _source(tmp_path)
    exported_path = tmp_path / "export.json"
    _write_export(exported_path, _export(source, (_item(),)))
    translation = dwg.make_translation(_item(), "Translated")
    base = dict(
        schema_version=1, operation="import", source_dwg=str(source), destination_dwg=str(tmp_path / "out.dwg"),
        export_json=str(exported_path), result_json=str(tmp_path / "result.json"), translations=(translation,), overwrite=False,
    )
    assert dwg.ImportTask(**base).destination_dwg.endswith("out.dwg")
    with pytest.raises(ValidationError, match="distinct"):
        dwg.ImportTask(**(base | {"destination_dwg": str(source)}))
    with pytest.raises(ValidationError, match=".dwg"):
        dwg.ImportTask(**(base | {"destination_dwg": str(tmp_path / "out.dxf")}))
    destination = tmp_path / "out.dwg"
    destination.write_bytes(b"existing")
    with pytest.raises(ValidationError, match="overwrite"):
        dwg.ImportTask(**base)
    assert destination.read_bytes() == b"existing"
    assert dwg.ImportTask(**(base | {"overwrite": True})).overwrite is True
    with pytest.raises(ValidationError, match="source_dwg and export_json"):
        dwg.ExportTask.model_validate({
            "schema_version": 1, "operation": "export", "source_dwg": str(source),
            "export_json": str(source).upper(), "layer_allow": (), "layer_deny": (), "overwrite": True,
        }, strict=True)
    for field, aliased_path in (
        ("destination_dwg", source),
        ("export_json", source),
        ("result_json", source),
        ("result_json", exported_path),
    ):
        with pytest.raises(ValidationError, match="distinct"):
            dwg.ImportTask(**(base | {field: str(aliased_path), "overwrite": True}))


def test_import_mapping_and_result_handles_must_exactly_match_export(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first, second = _item("A"), _item("B", "MText")
    exported = _export(source, (first, second))
    export_path = tmp_path / "export.json"
    _write_export(export_path, exported)
    translations = (dwg.make_translation(first, "One"), dwg.make_translation(second, "⟦MT_0001⟧Two⟦MT_0002⟧world⟦MT_0003⟧"))
    task = dwg.ImportTask(
        schema_version=1, operation="import", source_dwg=str(source), destination_dwg=str(tmp_path / "destination.dwg"),
        export_json=str(export_path), result_json=str(tmp_path / "result.json"), translations=translations, overwrite=False,
    )
    assert {item.handle for item in task.translations} == {"A", "B"}
    with pytest.raises(ValidationError, match="exactly"):
        dwg.ImportTask(**(task.model_dump() | {"translations": (translations[0],)}))

    destination = tmp_path / "destination.dwg"
    destination.write_bytes(b"verified translated fixture bytes")
    result = dwg.ImportResult(
        schema_version=1, operation="import_result", source_dwg=str(source), destination_dwg=str(tmp_path / "destination.dwg"),
        source_sha256=dwg.sha256_file(source), destination_sha256=dwg.sha256_file(destination),
        structure_before=exported.structure, structure_after=exported.structure, changed_handles=("A", "B"),
    )
    result_path = tmp_path / "result.json"
    result_path.write_text(result.model_dump_json(), encoding="utf-8")
    assert dwg.read_import_result(result_path, exported).changed_handles == ("A", "B")
    result_path.write_text(result.model_dump_json().replace('"B"', '"C"'), encoding="utf-8")
    with pytest.raises(dwg.ContractError, match="exactly"):
        dwg.read_import_result(result_path, exported)


def test_font_decision_is_optional_and_serializable(tmp_path: Path) -> None:
    source = _source(tmp_path)
    item = _item()
    decision = dwg.FontDecision(
        target_font_file="NotoSansSC-Regular.ttf",
        width_ratio=1.27,
        width_factor=0.79,
        review_required=True,
        review_reason="layout review",
    )
    translation = dwg.make_translation(item, "翻译", font_decision=decision)
    assert translation.font_decision == decision
    exported_path = tmp_path / "export.json"
    _write_export(exported_path, _export(source, (item,)))
    task = dwg.ImportTask(
        schema_version=1, operation="import", source_dwg=str(source),
        destination_dwg=str(tmp_path / "destination.dwg"), export_json=str(exported_path),
        result_json=str(tmp_path / "result.json"), translations=(translation,), overwrite=False,
    )
    assert task.translations[0].font_decision.target_font_file == "NotoSansSC-Regular.ttf"


@pytest.mark.parametrize("font_file", ["simsun.ttc", "MSYH.TTC"])
def test_font_decision_rejects_ttc_collections(font_file: str) -> None:
    with pytest.raises(ValidationError, match="TTC font collections"):
        dwg.FontDecision(target_font_file=font_file)


def test_import_result_requires_matching_paths_hashes_and_structure(tmp_path: Path) -> None:
    source = _source(tmp_path)
    exported = _export(source, (_item(),))
    destination = tmp_path / "destination.dwg"
    destination.write_bytes(b"real destination fixture bytes")
    result = dwg.ImportResult(
        schema_version=1, operation="import_result", source_dwg=str(source), destination_dwg=str(destination),
        source_sha256=dwg.sha256_file(source), destination_sha256=dwg.sha256_file(destination),
        structure_before=exported.structure, structure_after=exported.structure, changed_handles=("A",),
    )
    result_path = tmp_path / "result.json"
    result_path.write_text(result.model_dump_json(), encoding="utf-8")
    assert dwg.read_import_result(result_path, exported) == result
    for mutation, message in (
        ({"source_sha256": "0" * 64}, "source_sha256"),
        ({"destination_sha256": "0" * 64}, "destination_sha256"),
        ({"destination_dwg": str(tmp_path / "missing.dwg")}, "does not exist"),
        ({"structure_after": {"entity_count": 3, "layer_count": 1, "block_count": 2, "layout_count": 2, "xref_count": 0}}, "structure"),
    ):
        result_path.write_text(json.dumps(result.model_dump(mode="json") | mutation), encoding="utf-8")
        with pytest.raises(dwg.ContractError, match=message):
            dwg.read_import_result(result_path, exported)


def test_deterministic_task_json_hash_and_mtext_placeholder_round_trip(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    task = dwg.write_export_task(first_path, source_dwg=source, export_json=tmp_path / "export.json", layer_allow=("TEXT",))
    first = first_path.read_bytes()
    first_path.unlink()
    dwg.write_export_task(first_path, source_dwg=source, export_json=tmp_path / "export.json", layer_allow=("TEXT",))
    assert first_path.read_bytes() == first
    assert task.schema_version == 1
    assert dwg.sha256_text("中文 Text") == dwg.sha256_text("中文 Text")
    protected, sequences = dwg.protect_mtext(r"{A\C1;B\P\\C}")
    assert protected == "⟦MT_0001⟧A⟦MT_0002⟧B⟦MT_0003⟧⟦MT_0004⟧C⟦MT_0005⟧"
    assert dwg.restore_mtext(protected.replace("A", "Translated"), sequences) == r"{Translated\C1;B\P\\C}"
    with pytest.raises(dwg.ContractError):
        dwg.restore_mtext(protected.replace("⟦MT_0001⟧", ""), sequences)
    swapped = protected.replace("⟦MT_0001⟧", "⟦TEMP⟧").replace("⟦MT_0002⟧", "⟦MT_0001⟧").replace("⟦TEMP⟧", "⟦MT_0002⟧")
    with pytest.raises(dwg.ContractError, match="placeholders"):
        dwg.restore_mtext(swapped, sequences)
    assert "TR_EXPORT" in dwg.prepare_command_script("export", second_path)


def test_mtext_lowercase_paragraph_properties_are_protected_as_one_sequence() -> None:
    protected, sequences = dwg.protect_mtext(r"\pxsm1;CHINA\P\pi-1.44,l1.44;Note")

    assert protected == "⟦MT_0001⟧CHINA⟦MT_0002⟧⟦MT_0003⟧Note"
    assert [sequence.value for sequence in sequences] == [r"\pxsm1;", r"\P", r"\pi-1.44,l1.44;"]
    assert dwg.restore_mtext(protected, sequences) == r"\pxsm1;CHINA\P\pi-1.44,l1.44;Note"


def test_command_script_uses_gbk_crlf_and_preserves_chinese_task_path(tmp_path: Path) -> None:
    task_path = tmp_path / "中文任务.json"
    script_path = tmp_path / "运行脚本.scr"

    dwg.write_command_script(script_path, operation="export", task_json=task_path)

    payload = script_path.read_bytes()
    assert not payload.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in payload
    assert b"\n" not in payload.replace(b"\r\n", b"")
    assert payload.decode("gbk") == f'TR_EXPORT\r\n"{task_path}"\r\n'


def test_command_script_resolves_relative_task_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert dwg.prepare_command_script("import", Path("tasks") / "input.json") == (
        f'TR_IMPORT\n"{(tmp_path / "tasks" / "input.json").resolve()}"\n'
    )


def test_command_script_refuses_temporary_file_collision_without_replacing_target(tmp_path: Path) -> None:
    script_path = tmp_path / "command.scr"
    temporary = tmp_path / ".command.scr.tmp"
    temporary.write_bytes(b"in-progress")

    with pytest.raises(FileExistsError, match="temporary output exists"):
        dwg.write_command_script(script_path, operation="export", task_json=tmp_path / "task.json")

    assert not script_path.exists()
    assert temporary.read_bytes() == b"in-progress"


def test_command_script_can_explicitly_load_cadbridge_and_quit(tmp_path: Path) -> None:
    script = dwg.prepare_command_script(
        "import", tmp_path / "任务.json", netload_path=tmp_path / "CadBridge.dll", quit_after=True,
    )
    assert script == (
        'FILEDIA\n0\nCMDDIA\n0\n_.NETLOAD\n"'
        + str((tmp_path / "CadBridge.dll").resolve())
        + '"\nTR_IMPORT\n"'
        + str((tmp_path / "任务.json").resolve())
        + '"\n_.QUIT\n'
    )


def test_command_script_refuses_overwrite_and_unencodable_task_path(tmp_path: Path) -> None:
    script_path = tmp_path / "command.scr"
    script_path.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="output exists"):
        dwg.write_command_script(script_path, operation="import", task_json=tmp_path / "任务.json")
    assert script_path.read_bytes() == b"existing"
    with pytest.raises(dwg.ContractError, match="CP936/GBK"):
        dwg.write_command_script(
            tmp_path / "unencodable.scr", operation="export", task_json=tmp_path / "emoji-😀.json"
        )


def test_python_adapter_has_no_dwg_editing_backend() -> None:
    source = Path(dwg.__file__).read_text(encoding="utf-8").casefold()
    forbidden = ("pyautocad", "ezdxf", "autocad.application", "win32com", "comtypes", "autolisp")
    assert not any(token in source for token in forbidden)
    assert "read or writes dwg bytes" not in source
