"""Strict, versioned JSON orchestration for the native AutoCAD CadBridge.

This module never reads or writes DWG bytes. AutoCAD DatabaseServices is the
only DWG backend; Python prepares tasks and validates bridge results.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1
EXPORT_OPERATION = "export"
EXPORT_RESULT_OPERATION = "export_result"
IMPORT_OPERATION = "import"
IMPORT_RESULT_OPERATION = "import_result"

Handle = Annotated[str, Field(pattern=r"^[1-9A-F][0-9A-F]*$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EntityType = Literal["DBText", "MText", "AttributeReference"]
_MT_TOKEN_RE = re.compile(r"⟦MT_\d{4}⟧")
# Lower-case \p...; carries paragraph properties; upper-case \P is a line break.
_MT_TERMINATED_CODES = frozenset("ACFHQRSTWXacfhpqrstwx")


class ContractError(ValueError):
    """A bridge task or result violates the Stage 4 contract."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ProtectedSequence(_StrictModel):
    token: Annotated[str, Field(pattern=r"^⟦MT_\d{4}⟧$")]
    value: str


class PointData(_StrictModel):
    x: float
    y: float
    z: float


class BoundsData(_StrictModel):
    min: PointData
    max: PointData


class TextMetadata(_StrictModel):
    position: PointData
    normal: PointData
    rotation: float
    width: float
    height: float
    width_factor: float
    style_name: str
    style_handle: Handle
    color_index: int
    color_method: str
    block_handle: Handle | None
    attribute_tag: str | None
    font_file: str | None = None
    big_font_file: str | None = None
    is_shape_file: bool = False
    is_vertical: bool = False
    style_text_size: float = 0.0
    style_x_scale: float = 1.0
    style_oblique_angle: float = 0.0


class FontDecision(_StrictModel):
    """Optional, deterministic font/style decision applied by CadBridge."""

    target_font_file: str | None = None
    target_big_font_file: str | None = None
    target_style_name: str | None = None
    width_factor: float | None = Field(default=None, gt=0)
    width_ratio: float | None = Field(default=None, gt=0)
    review_required: bool = False
    review_reason: str | None = None
    apply_to_mtext: bool = False

    @field_validator("target_font_file")
    @classmethod
    def validate_target_font_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name or Path(name).name != name:
            raise ValueError("target_font_file must be a font file name, not a path")
        if Path(name).suffix.lower() == ".ttc":
            raise ValueError("TTC font collections are not reliable for CAD writeback; use a single-face TTF/OTF font")
        return name

    @model_validator(mode="after")
    def validate_target(self) -> "FontDecision":
        if not self.target_font_file and not self.target_style_name and self.width_factor is None:
            raise ValueError("font decision must specify a target font, style, or width factor")
        return self


class StructuralSnapshot(_StrictModel):
    entity_count: Annotated[int, Field(ge=0)]
    layer_count: Annotated[int, Field(ge=0)]
    block_count: Annotated[int, Field(ge=0)]
    layout_count: Annotated[int, Field(ge=0)]
    xref_count: Annotated[int, Field(ge=0)]


class TextItem(_StrictModel):
    handle: Handle
    entity_type: EntityType
    space: str
    layer: str
    source_text: str
    source_hash: Sha256
    bounds: BoundsData | None
    metadata: TextMetadata
    protected_sequences: tuple[ProtectedSequence, ...]

    @model_validator(mode="after")
    def validate_source(self) -> "TextItem":
        raw = restore_mtext(self.source_text, self.protected_sequences) if self.entity_type == "MText" else self.source_text
        if self.entity_type != "MText" and self.protected_sequences:
            raise ValueError("protected_sequences are only valid for MText")
        if sha256_text(raw) != self.source_hash:
            raise ValueError("source_hash does not match source_text")
        return self


class TranslationItem(_StrictModel):
    handle: Handle
    entity_type: EntityType
    source_text: str
    source_hash: Sha256
    translated_text: str
    protected_sequences: tuple[ProtectedSequence, ...]
    font_decision: FontDecision | None = None

    @model_validator(mode="after")
    def validate_translation(self) -> "TranslationItem":
        if not self.translated_text.strip():
            raise ValueError("translated_text must not be empty")
        raw = restore_mtext(self.source_text, self.protected_sequences) if self.entity_type == "MText" else self.source_text
        if self.entity_type != "MText" and self.protected_sequences:
            raise ValueError("protected_sequences are only valid for MText")
        if sha256_text(raw) != self.source_hash:
            raise ValueError("source_hash does not match source_text")
        if self.entity_type == "MText":
            validate_mtext_placeholders(self.translated_text, self.protected_sequences)
        return self


class ExportTask(_StrictModel):
    schema_version: Literal[1]
    operation: Literal["export"]
    source_dwg: str
    export_json: str
    layer_allow: tuple[str, ...]
    layer_deny: tuple[str, ...]
    overwrite: bool

    @field_validator("source_dwg")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _source_dwg(value)

    @field_validator("export_json")
    @classmethod
    def validate_export_path(cls, value: str) -> str:
        return _output_path(value, overwrite=None)

    @field_validator("layer_allow", "layer_deny")
    @classmethod
    def validate_layers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not layer.strip() for layer in value):
            raise ValueError("layer names must not be empty")
        if len({layer.casefold() for layer in value}) != len(value):
            raise ValueError("layer list contains duplicates")
        return value

    @model_validator(mode="after")
    def validate_output_policy(self) -> "ExportTask":
        if _same_path(self.source_dwg, self.export_json):
            raise ValueError("source_dwg and export_json must be distinct")
        _ensure_output_available(self.export_json, self.overwrite)
        return self


class ExportDocument(_StrictModel):
    schema_version: Literal[1]
    operation: Literal["export_result"]
    source_dwg: str
    source_sha256: Sha256
    structure: StructuralSnapshot
    items: tuple[TextItem, ...]

    @field_validator("source_dwg")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        return _dwg_path(value, "source_dwg")

    @model_validator(mode="after")
    def validate_handles(self) -> "ExportDocument":
        _require_unique_handles(self.items, "export items")
        return self


class ImportTask(_StrictModel):
    schema_version: Literal[1]
    operation: Literal["import"]
    source_dwg: str
    destination_dwg: str
    export_json: str
    result_json: str
    translations: tuple[TranslationItem, ...]
    overwrite: bool

    @field_validator("source_dwg")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _source_dwg(value)

    @field_validator("destination_dwg")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        return _dwg_path(value, "destination_dwg")

    @field_validator("export_json")
    @classmethod
    def validate_export_json(cls, value: str) -> str:
        path = str(Path(value).expanduser().resolve())
        if not Path(path).is_file():
            raise ValueError("export_json does not exist")
        return path

    @field_validator("result_json")
    @classmethod
    def validate_result_json(cls, value: str) -> str:
        return _output_path(value, overwrite=None)

    @model_validator(mode="after")
    def validate_paths_and_mapping(self) -> "ImportTask":
        _require_distinct_paths(
            ("source_dwg", self.source_dwg),
            ("destination_dwg", self.destination_dwg),
            ("export_json", self.export_json),
            ("result_json", self.result_json),
        )
        _ensure_output_available(self.destination_dwg, self.overwrite)
        _ensure_output_available(self.result_json, self.overwrite)
        _require_unique_handles(self.translations, "translations")
        exported = read_export_document(self.export_json)
        if not _same_path(exported.source_dwg, self.source_dwg):
            raise ValueError("export source_dwg does not match import source_dwg")
        _validate_exact_mapping(exported, self.translations)
        return self


class ImportResult(_StrictModel):
    schema_version: Literal[1]
    operation: Literal["import_result"]
    source_dwg: str
    destination_dwg: str
    source_sha256: Sha256
    destination_sha256: Sha256
    structure_before: StructuralSnapshot
    structure_after: StructuralSnapshot
    changed_handles: tuple[Handle, ...]

    @field_validator("source_dwg", "destination_dwg")
    @classmethod
    def validate_dwg_paths(cls, value: str, info) -> str:
        return _dwg_path(value, info.field_name)

    @model_validator(mode="after")
    def validate_result(self) -> "ImportResult":
        if _same_path(self.source_dwg, self.destination_dwg):
            raise ValueError("source_dwg and destination_dwg must be distinct")
        if len(set(self.changed_handles)) != len(self.changed_handles):
            raise ValueError("changed_handles contains duplicates")
        if self.structure_before != self.structure_after:
            raise ValueError("destination structure differs from source structure")
        return self


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash a file read-only; suitable for proving that a source DWG is unchanged."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protect_mtext(contents: str) -> tuple[str, tuple[ProtectedSequence, ...]]:
    """Protect MText braces and backslash control sequences deterministically."""
    if _MT_TOKEN_RE.search(contents):
        raise ContractError("MText contains a reserved CadBridge placeholder")
    output: list[str] = []
    sequences: list[ProtectedSequence] = []
    index = 0
    while index < len(contents):
        length = _mtext_sequence_length(contents, index)
        if not length:
            output.append(contents[index])
            index += 1
            continue
        token = f"⟦MT_{len(sequences) + 1:04d}⟧"
        sequences.append(ProtectedSequence(token=token, value=contents[index:index + length]))
        output.append(token)
        index += length
    return "".join(output), tuple(sequences)


def validate_mtext_placeholders(text: str, sequences: Sequence[ProtectedSequence]) -> None:
    expected = [sequence.token for sequence in sequences]
    if len(set(expected)) != len(expected):
        raise ContractError("duplicate MText placeholder token")
    actual = _MT_TOKEN_RE.findall(text)
    if actual != expected or any(text.count(token) != 1 for token in expected):
        raise ContractError("MText placeholders were changed, duplicated, removed, or added")


def restore_mtext(text: str, sequences: Sequence[ProtectedSequence]) -> str:
    validate_mtext_placeholders(text, sequences)
    restored = text
    for sequence in sequences:
        restored = restored.replace(sequence.token, sequence.value)
    if _MT_TOKEN_RE.search(restored):
        raise ContractError("unexpected MText placeholder remains after restoration")
    return restored


def read_export_document(path: str | Path) -> ExportDocument:
    return _read_model(path, ExportDocument)


def read_import_result(path: str | Path, exported: ExportDocument) -> ImportResult:
    result = _read_model(path, ImportResult)
    if not _same_path(result.source_dwg, exported.source_dwg):
        raise ContractError("import result source_dwg does not match export")
    if result.source_sha256 != exported.source_sha256:
        raise ContractError("import result source_sha256 does not match export")
    try:
        current_source_hash = sha256_file(exported.source_dwg)
    except OSError as error:
        raise ContractError(f"cannot hash export source_dwg: {error}") from error
    if current_source_hash != exported.source_sha256:
        raise ContractError("current source_dwg hash does not match export")
    destination = Path(result.destination_dwg)
    if not destination.is_file():
        raise ContractError("import result destination_dwg does not exist")
    if sha256_file(destination) != result.destination_sha256:
        raise ContractError("import result destination_sha256 does not match destination_dwg")
    if result.structure_before != exported.structure or result.structure_after != exported.structure:
        raise ContractError("import result structure does not match export")
    exported_handles = {item.handle for item in exported.items}
    if set(result.changed_handles) != exported_handles or len(result.changed_handles) != len(exported_handles):
        raise ContractError("import result changed_handles do not exactly match exported handles")
    return result


def make_translation(
    item: TextItem,
    translated_text: str,
    *,
    font_decision: FontDecision | None = None,
) -> TranslationItem:
    return TranslationItem(
        handle=item.handle,
        entity_type=item.entity_type,
        source_text=item.source_text,
        source_hash=item.source_hash,
        translated_text=translated_text,
        protected_sequences=item.protected_sequences,
        font_decision=font_decision,
    )


def write_export_task(
    path: str | Path,
    *,
    source_dwg: str | Path,
    export_json: str | Path,
    layer_allow: Sequence[str] = (),
    layer_deny: Sequence[str] = (),
    overwrite: bool = False,
) -> ExportTask:
    task = ExportTask(
        schema_version=SCHEMA_VERSION,
        operation=EXPORT_OPERATION,
        source_dwg=str(source_dwg),
        export_json=str(export_json),
        layer_allow=tuple(layer_allow),
        layer_deny=tuple(layer_deny),
        overwrite=overwrite,
    )
    _write_model(path, task, overwrite=False)
    return task


def write_import_task(
    path: str | Path,
    *,
    source_dwg: str | Path,
    destination_dwg: str | Path,
    export_json: str | Path,
    result_json: str | Path,
    translations: Sequence[TranslationItem],
    overwrite: bool = False,
) -> ImportTask:
    task = ImportTask(
        schema_version=SCHEMA_VERSION,
        operation=IMPORT_OPERATION,
        source_dwg=str(source_dwg),
        destination_dwg=str(destination_dwg),
        export_json=str(export_json),
        result_json=str(result_json),
        translations=tuple(translations),
        overwrite=overwrite,
    )
    _write_model(path, task, overwrite=False)
    return task


def prepare_command_script(
    operation: Literal["export", "import"],
    task_json: str | Path,
    *,
    netload_path: str | Path | None = None,
    quit_after: bool = False,
) -> str:
    """Build an AutoCAD command script, optionally including explicit CadBridge loading."""
    command = "TR_EXPORT" if operation == "export" else "TR_IMPORT"
    task_path = str(Path(task_json).expanduser().resolve())
    if '"' in task_path:
        raise ContractError("task path contains an unsupported quote")
    lines: list[str] = []
    if netload_path is not None:
        assembly_path = str(Path(netload_path).expanduser().resolve())
        if '"' in assembly_path:
            raise ContractError("netload path contains an unsupported quote")
        lines.extend(["FILEDIA", "0", "CMDDIA", "0", "_.NETLOAD", f'"{assembly_path}"'])
    lines.extend([command, f'"{task_path}"'])
    if quit_after:
        lines.append("_.QUIT")
    return "\n".join(lines) + "\n"


def write_command_script(
    path: str | Path,
    *,
    operation: Literal["export", "import"],
    task_json: str | Path,
    overwrite: bool = False,
    netload_path: str | Path | None = None,
    quit_after: bool = False,
) -> None:
    """Write an AutoCAD command script encoded as CP936/GBK with CRLF line endings."""
    target = Path(path).expanduser().resolve()
    if target.exists() and not overwrite:
        raise FileExistsError(f"output exists: {target}")
    script = prepare_command_script(
        operation, task_json, netload_path=netload_path, quit_after=quit_after,
    ).replace("\n", "\r\n")
    try:
        payload = script.encode("gbk")
    except UnicodeEncodeError as error:
        raise ContractError("command script contains text that cannot be encoded as CP936/GBK") from error
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary output exists: {temporary}")
    try:
        temporary.write_bytes(payload)
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_exact_mapping(exported: ExportDocument, translations: Sequence[TranslationItem]) -> None:
    by_handle = {item.handle: item for item in exported.items}
    if {item.handle for item in translations} != set(by_handle) or len(translations) != len(by_handle):
        raise ValueError("translations must correspond exactly to all exported handles")
    for translation in translations:
        source = by_handle[translation.handle]
        if (
            translation.entity_type != source.entity_type
            or translation.source_text != source.source_text
            or translation.source_hash != source.source_hash
            or translation.protected_sequences != source.protected_sequences
        ):
            raise ValueError(f"translation source contract differs for handle {translation.handle}")


def _mtext_sequence_length(text: str, index: int) -> int:
    if text[index] in "{}":
        return 1
    if text[index] != "\\" or index + 1 >= len(text):
        return 0
    if text[index + 1] in _MT_TERMINATED_CODES:
        terminator = text.find(";", index + 2)
        return len(text) - index if terminator < 0 else terminator - index + 1
    return 2


def _read_model(path: str | Path, model_type):
    try:
        return model_type.model_validate_json(Path(path).read_text(encoding="utf-8"), strict=True)
    except (OSError, ValueError) as error:
        raise ContractError(str(error)) from error


def _write_model(path: str | Path, model: BaseModel, *, overwrite: bool) -> None:
    target = Path(path).expanduser().resolve()
    if target.exists() and not overwrite:
        raise FileExistsError(f"output exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = target.with_name(f".{target.name}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary output exists: {temporary}")
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_dwg(value: str) -> str:
    path = _dwg_path(value, "source_dwg")
    if not Path(path).is_file():
        raise ValueError("source_dwg does not exist")
    return path


def _dwg_path(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")
    path = Path(value).expanduser().resolve()
    if path.suffix.casefold() != ".dwg":
        raise ValueError(f"{field} must have a .dwg extension")
    return str(path)


def _output_path(value: str, overwrite: bool | None) -> str:
    if not value.strip():
        raise ValueError("output path must not be empty")
    path = str(Path(value).expanduser().resolve())
    if overwrite is not None:
        _ensure_output_available(path, overwrite)
    return path


def _ensure_output_available(path: str, overwrite: bool) -> None:
    if Path(path).exists() and not overwrite:
        raise ValueError(f"output exists and overwrite is false: {path}")


def _same_path(left: str | Path, right: str | Path) -> bool:
    return str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()


def _require_distinct_paths(*named_paths: tuple[str, str]) -> None:
    for index, (left_name, left_path) in enumerate(named_paths):
        for right_name, right_path in named_paths[index + 1:]:
            if _same_path(left_path, right_path):
                raise ValueError(f"{left_name} and {right_name} must be distinct")


def _require_unique_handles(items: Sequence[TextItem | TranslationItem], label: str) -> None:
    handles = [item.handle for item in items]
    if len(set(handles)) != len(handles):
        raise ValueError(f"{label} contain duplicate handles")
