using System.Text.Json.Serialization;

namespace CadBridge;

internal static class Contract
{
    public const int SchemaVersion = 1;
    public const string ExportOperation = "export";
    public const string ExportResultOperation = "export_result";
    public const string ImportOperation = "import";
    public const string ImportResultOperation = "import_result";
}

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record ExportTask(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("operation")] string Operation,
    [property: JsonPropertyName("source_dwg")] string SourceDwg,
    [property: JsonPropertyName("export_json")] string ExportJson,
    [property: JsonPropertyName("layer_allow")] string[] LayerAllow,
    [property: JsonPropertyName("layer_deny")] string[] LayerDeny,
    [property: JsonPropertyName("overwrite")] bool Overwrite);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record ImportTask(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("operation")] string Operation,
    [property: JsonPropertyName("source_dwg")] string SourceDwg,
    [property: JsonPropertyName("destination_dwg")] string DestinationDwg,
    [property: JsonPropertyName("export_json")] string ExportJson,
    [property: JsonPropertyName("result_json")] string ResultJson,
    [property: JsonPropertyName("translations")] TranslationItem[] Translations,
    [property: JsonPropertyName("overwrite")] bool Overwrite);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record TranslationItem(
    [property: JsonPropertyName("handle")] string Handle,
    [property: JsonPropertyName("entity_type")] string EntityType,
    [property: JsonPropertyName("source_text")] string SourceText,
    [property: JsonPropertyName("source_hash")] string SourceHash,
    [property: JsonPropertyName("translated_text")] string TranslatedText,
    [property: JsonPropertyName("protected_sequences")] ProtectedSequence[] ProtectedSequences,
    [property: JsonPropertyName("font_decision")] FontDecision? FontDecision = null);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record FontDecision(
    [property: JsonPropertyName("target_font_file")] string? TargetFontFile = null,
    [property: JsonPropertyName("target_big_font_file")] string? TargetBigFontFile = null,
    [property: JsonPropertyName("target_style_name")] string? TargetStyleName = null,
    [property: JsonPropertyName("width_factor")] double? WidthFactor = null,
    [property: JsonPropertyName("width_ratio")] double? WidthRatio = null,
    [property: JsonPropertyName("review_required")] bool ReviewRequired = false,
    [property: JsonPropertyName("review_reason")] string? ReviewReason = null,
    [property: JsonPropertyName("apply_to_mtext")] bool ApplyToMText = false);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record ProtectedSequence(
    [property: JsonPropertyName("token")] string Token,
    [property: JsonPropertyName("value")] string Value);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record PointData(
    [property: JsonPropertyName("x")] double X,
    [property: JsonPropertyName("y")] double Y,
    [property: JsonPropertyName("z")] double Z);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record BoundsData(
    [property: JsonPropertyName("min")] PointData Min,
    [property: JsonPropertyName("max")] PointData Max);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record TextMetadata(
    [property: JsonPropertyName("position")] PointData Position,
    [property: JsonPropertyName("normal")] PointData Normal,
    [property: JsonPropertyName("rotation")] double Rotation,
    [property: JsonPropertyName("width")] double Width,
    [property: JsonPropertyName("height")] double Height,
    [property: JsonPropertyName("width_factor")] double WidthFactor,
    [property: JsonPropertyName("style_name")] string StyleName,
    [property: JsonPropertyName("style_handle")] string StyleHandle,
    [property: JsonPropertyName("color_index")] short ColorIndex,
    [property: JsonPropertyName("color_method")] string ColorMethod,
    [property: JsonPropertyName("block_handle")] string? BlockHandle,
    [property: JsonPropertyName("attribute_tag")] string? AttributeTag,
    [property: JsonPropertyName("font_file")] string? FontFile = null,
    [property: JsonPropertyName("big_font_file")] string? BigFontFile = null,
    [property: JsonPropertyName("is_shape_file")] bool IsShapeFile = false,
    [property: JsonPropertyName("is_vertical")] bool IsVertical = false,
    [property: JsonPropertyName("style_text_size")] double StyleTextSize = 0,
    [property: JsonPropertyName("style_x_scale")] double StyleXScale = 1,
    [property: JsonPropertyName("style_oblique_angle")] double StyleObliqueAngle = 0);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record TextItem(
    [property: JsonPropertyName("handle")] string Handle,
    [property: JsonPropertyName("entity_type")] string EntityType,
    [property: JsonPropertyName("space")] string Space,
    [property: JsonPropertyName("layer")] string Layer,
    [property: JsonPropertyName("source_text")] string SourceText,
    [property: JsonPropertyName("source_hash")] string SourceHash,
    [property: JsonPropertyName("bounds")] BoundsData? Bounds,
    [property: JsonPropertyName("metadata")] TextMetadata Metadata,
    [property: JsonPropertyName("protected_sequences")] ProtectedSequence[] ProtectedSequences);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record StructuralSnapshot(
    [property: JsonPropertyName("entity_count")] long EntityCount,
    [property: JsonPropertyName("layer_count")] long LayerCount,
    [property: JsonPropertyName("block_count")] long BlockCount,
    [property: JsonPropertyName("layout_count")] long LayoutCount,
    [property: JsonPropertyName("xref_count")] long XrefCount);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record ExportDocument(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("operation")] string Operation,
    [property: JsonPropertyName("source_dwg")] string SourceDwg,
    [property: JsonPropertyName("source_sha256")] string SourceSha256,
    [property: JsonPropertyName("structure")] StructuralSnapshot Structure,
    [property: JsonPropertyName("items")] TextItem[] Items);

[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
internal sealed record ImportResult(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("operation")] string Operation,
    [property: JsonPropertyName("source_dwg")] string SourceDwg,
    [property: JsonPropertyName("destination_dwg")] string DestinationDwg,
    [property: JsonPropertyName("source_sha256")] string SourceSha256,
    [property: JsonPropertyName("destination_sha256")] string DestinationSha256,
    [property: JsonPropertyName("structure_before")] StructuralSnapshot StructureBefore,
    [property: JsonPropertyName("structure_after")] StructuralSnapshot StructureAfter,
    [property: JsonPropertyName("changed_handles")] string[] ChangedHandles);
