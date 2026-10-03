using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

namespace CadBridge;

internal static class DwgBridge
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = false,
        WriteIndented = true,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };

    private static readonly HashSet<string> SupportedTypes = new(StringComparer.Ordinal)
    {
        nameof(DBText), nameof(MText), nameof(AttributeReference)
    };

    public static void Export(string taskPath)
    {
        var task = ReadJson<ExportTask>(taskPath);
        ValidateHeader(task.SchemaVersion, task.Operation, Contract.ExportOperation);
        var source = ValidateDwgSource(task.SourceDwg);
        var exportJson = Path.GetFullPath(RequireText(task.ExportJson, "export_json"));
        RequireDistinctPaths(("source_dwg", source), ("export_json", exportJson));
        EnsureOutputAvailable(exportJson, task.Overwrite, "export_json");
        ValidateLayerLists(task.LayerAllow, task.LayerDeny);

        var hashBefore = Sha256File(source);
        ExportDocument document;
        using (var database = OpenReadOnly(source))
        using (new WorkingDatabaseScope(database))
        {
            var items = ExtractItems(database, task.LayerAllow, task.LayerDeny);
            var structure = CaptureStructure(database);
            document = new ExportDocument(
                Contract.SchemaVersion,
                Contract.ExportResultOperation,
                source,
                hashBefore,
                structure,
                items.OrderBy(item => HandleValue(item.Handle)).ToArray());
        }
        if (!string.Equals(hashBefore, Sha256File(source), StringComparison.Ordinal))
            throw new InvalidDataException("Source DWG changed during read-only export.");
        WriteJsonAtomic(exportJson, document, task.Overwrite);
    }

    public static void Import(string taskPath)
    {
        var task = ReadJson<ImportTask>(taskPath);
        ValidateHeader(task.SchemaVersion, task.Operation, Contract.ImportOperation);
        var source = ValidateDwgSource(task.SourceDwg);
        var destination = ValidateDestination(source, task.DestinationDwg, task.Overwrite);
        var exportJson = Path.GetFullPath(RequireText(task.ExportJson, "export_json"));
        var resultJson = Path.GetFullPath(RequireText(task.ResultJson, "result_json"));
        RequireDistinctPaths(
            ("source_dwg", source),
            ("destination_dwg", destination),
            ("export_json", exportJson),
            ("result_json", resultJson));
        EnsureOutputAvailable(resultJson, task.Overwrite, "result_json");
        var export = ReadJson<ExportDocument>(exportJson);
        ValidateExportDocument(export, source);
        var translations = ValidateTranslations(export, task.Translations);

        var sourceHash = Sha256File(source);
        if (!string.Equals(sourceHash, export.SourceSha256, StringComparison.Ordinal))
            throw new InvalidDataException("Source DWG hash does not match the export document.");

        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        Directory.CreateDirectory(Path.GetDirectoryName(resultJson)!);
        var temporaryDwg = Path.Combine(Path.GetDirectoryName(destination)!, $".{Path.GetFileNameWithoutExtension(destination)}.{Guid.NewGuid():N}.dwg");
        var temporaryResult = Path.Combine(Path.GetDirectoryName(resultJson)!, $".{Path.GetFileName(resultJson)}.{Guid.NewGuid():N}.tmp");
        try
        {
            StructuralSnapshot before;
            DwgVersion sourceVersion;
            using (var database = OpenReadOnly(source))
            using (new WorkingDatabaseScope(database))
            {
                before = CaptureStructure(database);
                RequireSameStructure(export.Structure, before, "Source structure differs from export snapshot.");
                sourceVersion = database.OriginalFileVersion;
                ApplyTranslations(database, translations);
                var afterEdit = CaptureStructure(database);
                RequireSameStructure(before, afterEdit, "Text updates changed source database structure.");
                database.SaveAs(temporaryDwg, sourceVersion);
            }

            StructuralSnapshot reopened;
            using (var verification = OpenReadOnly(temporaryDwg))
            using (new WorkingDatabaseScope(verification))
            {
                reopened = CaptureStructure(verification);
                RequireSameStructure(before, reopened, "Saved destination structure differs from source.");
                VerifyChangedText(verification, translations);
            }

            if (!string.Equals(sourceHash, Sha256File(source), StringComparison.Ordinal))
                throw new InvalidDataException("Source DWG changed during import.");

            var result = new ImportResult(
                Contract.SchemaVersion,
                Contract.ImportResultOperation,
                source,
                destination,
                sourceHash,
                Sha256File(temporaryDwg),
                before,
                reopened,
                translations.Keys.OrderBy(HandleValue).ToArray());
            File.WriteAllText(temporaryResult, Serialize(result), new UTF8Encoding(false));
            PublishOutputs(temporaryDwg, destination, temporaryResult, resultJson);
        }
        catch
        {
            DeleteIfExists(temporaryDwg);
            DeleteIfExists(temporaryResult);
            throw;
        }
    }

    private static Database OpenReadOnly(string path)
    {
        var database = new Database(false, true);
        try
        {
            database.ReadDwgFile(path, FileOpenMode.OpenForReadAndAllShare, false, null);
            return database;
        }
        catch
        {
            database.Dispose();
            throw;
        }
    }

    private static TextItem[] ExtractItems(Database database, string[] allow, string[] deny)
    {
        var allowed = new HashSet<string>(allow, StringComparer.OrdinalIgnoreCase);
        var denied = new HashSet<string>(deny, StringComparer.OrdinalIgnoreCase);
        var items = new List<TextItem>();
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        var layouts = (DBDictionary)transaction.GetObject(database.LayoutDictionaryId, OpenMode.ForRead);
        foreach (DBDictionaryEntry entry in layouts)
        {
            var layout = (Layout)transaction.GetObject(entry.Value, OpenMode.ForRead);
            var space = layout.ModelType ? "Model" : $"Paper:{layout.LayoutName}";
            var record = (BlockTableRecord)transaction.GetObject(layout.BlockTableRecordId, OpenMode.ForRead);
            foreach (ObjectId id in record)
            {
                var entity = transaction.GetObject(id, OpenMode.ForRead, false) as Entity;
                if (entity is null || !LayerAccepted(entity, transaction, allowed, denied) || entity.HasFields)
                    continue;
                if (entity.GetType() == typeof(DBText) && !ContainsUnsupportedTextControls(((DBText)entity).TextString))
                    items.Add(CreateDbText((DBText)entity, space, transaction));
                else if (entity.GetType() == typeof(MText) && !ContainsUnsupportedTextControls(((MText)entity).Contents))
                    items.Add(CreateMText((MText)entity, space, transaction));
                else if (entity.GetType() == typeof(BlockReference))
                    ExtractAttributes((BlockReference)entity, space, transaction, allowed, denied, items);
            }
        }
        ExtractBlockDefinitionText(database, transaction, allowed, denied, items);
        transaction.Commit();
        return items.ToArray();
    }

    private static void ExtractBlockDefinitionText(Database database, Transaction transaction,
        HashSet<string> allowed, HashSet<string> denied, List<TextItem> items)
    {
        var blocks = (BlockTable)transaction.GetObject(database.BlockTableId, OpenMode.ForRead);
        foreach (ObjectId blockId in blocks)
        {
            var definition = (BlockTableRecord)transaction.GetObject(blockId, OpenMode.ForRead);
            if (definition.IsLayout || definition.IsFromExternalReference || definition.IsFromOverlayReference)
                continue;
            var space = $"Block:{definition.Name}";
            foreach (ObjectId id in definition)
            {
                var entity = transaction.GetObject(id, OpenMode.ForRead, false) as Entity;
                if (entity is null || !LayerAccepted(entity, transaction, allowed, denied) || entity.HasFields)
                    continue;
                if (entity.GetType() == typeof(DBText) && !ContainsUnsupportedTextControls(((DBText)entity).TextString))
                    items.Add(CreateDbText((DBText)entity, space, transaction));
                else if (entity.GetType() == typeof(MText) && !ContainsUnsupportedTextControls(((MText)entity).Contents))
                    items.Add(CreateMText((MText)entity, space, transaction));
            }
        }
    }

    private static void ExtractAttributes(BlockReference block, string space, Transaction transaction,
        HashSet<string> allowed, HashSet<string> denied, List<TextItem> items)
    {
        var definition = (BlockTableRecord)transaction.GetObject(block.BlockTableRecord, OpenMode.ForRead);
        if (definition.IsFromExternalReference || definition.IsFromOverlayReference)
            return;
        foreach (ObjectId id in block.AttributeCollection)
        {
            var attribute = transaction.GetObject(id, OpenMode.ForRead, false) as AttributeReference;
            if (attribute is null || attribute.GetType() != typeof(AttributeReference) || attribute.HasFields ||
                !LayerAccepted(attribute, transaction, allowed, denied) || ContainsUnsupportedTextControls(attribute.TextString))
                continue;
            items.Add(CreateAttribute(attribute, block, space, transaction));
        }
    }

    private static bool LayerAccepted(Entity entity, Transaction transaction,
        HashSet<string> allowed, HashSet<string> denied)
    {
        var layer = (LayerTableRecord)transaction.GetObject(entity.LayerId, OpenMode.ForRead);
        return !layer.IsLocked && (allowed.Count == 0 || allowed.Contains(entity.Layer)) && !denied.Contains(entity.Layer);
    }

    private static TextItem CreateDbText(DBText text, string space, Transaction transaction)
    {
        var raw = text.TextString;
        return new TextItem(HandleText(text), nameof(DBText), space, text.Layer, raw, Sha256Text(raw),
            GetBounds(text), Metadata(text.Position, text.Normal, text.Rotation, 0, text.Height, text.WidthFactor,
                text.TextStyleId, text.Color, null, null, transaction), Array.Empty<ProtectedSequence>());
    }

    private static TextItem CreateMText(MText text, string space, Transaction transaction)
    {
        var raw = text.Contents;
        var protectedText = MTextProtector.Protect(raw);
        return new TextItem(HandleText(text), nameof(MText), space, text.Layer, protectedText.Text, Sha256Text(raw),
            GetBounds(text), Metadata(text.Location, text.Normal, text.Rotation, text.Width, text.TextHeight, 1,
                text.TextStyleId, text.Color, null, null, transaction), protectedText.Sequences);
    }

    private static TextItem CreateAttribute(AttributeReference text, BlockReference block, string space, Transaction transaction)
    {
        var raw = text.TextString;
        return new TextItem(HandleText(text), nameof(AttributeReference), space, text.Layer, raw, Sha256Text(raw),
            GetBounds(text), Metadata(text.Position, text.Normal, text.Rotation, 0, text.Height, text.WidthFactor,
                text.TextStyleId, text.Color, HandleText(block), text.Tag, transaction), Array.Empty<ProtectedSequence>());
    }

    private static TextMetadata Metadata(Point3d position, Vector3d normal, double rotation, double width,
        double height, double widthFactor, ObjectId styleId, Color color, string? blockHandle,
        string? attributeTag, Transaction transaction)
    {
        var style = (TextStyleTableRecord)transaction.GetObject(styleId, OpenMode.ForRead);
        return new TextMetadata(Point(position), Point(normal), rotation, width, height, widthFactor,
            style.Name, HandleText(style), checked((short)color.ColorIndex), color.ColorMethod.ToString(),
            blockHandle, attributeTag, style.FileName, style.BigFontFileName, style.IsShapeFile,
            style.IsVertical, style.TextSize, style.XScale, style.ObliquingAngle);
    }

    private static BoundsData? GetBounds(Entity entity)
    {
        try
        {
            var bounds = entity.GeometricExtents;
            return new BoundsData(Point(bounds.MinPoint), Point(bounds.MaxPoint));
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            return null;
        }
    }

    private static PointData Point(Point3d point) => new(point.X, point.Y, point.Z);
    private static PointData Point(Vector3d vector) => new(vector.X, vector.Y, vector.Z);

    private static StructuralSnapshot CaptureStructure(Database database)
    {
        long entityCount = 0, blockCount = 0, xrefCount = 0;
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        var blocks = (BlockTable)transaction.GetObject(database.BlockTableId, OpenMode.ForRead);
        foreach (ObjectId id in blocks)
        {
            var record = (BlockTableRecord)transaction.GetObject(id, OpenMode.ForRead);
            blockCount++;
            if (record.IsFromExternalReference || record.IsFromOverlayReference)
                xrefCount++;
            foreach (ObjectId ignored in record)
                entityCount++;
        }
        var layers = (LayerTable)transaction.GetObject(database.LayerTableId, OpenMode.ForRead);
        long layerCount = layers.Cast<ObjectId>().LongCount();
        var layouts = (DBDictionary)transaction.GetObject(database.LayoutDictionaryId, OpenMode.ForRead);
        long layoutCount = 0;
        foreach (DBDictionaryEntry entry in layouts)
            layoutCount++;
        transaction.Commit();
        return new StructuralSnapshot(entityCount, layerCount, blockCount, layoutCount, xrefCount);
    }

    private static void ApplyTranslations(Database database, IReadOnlyDictionary<string, TranslationItem> translations)
    {
        using var transaction = database.TransactionManager.StartTransaction();
        foreach (var (handle, item) in translations)
        {
            var entity = GetEntity(database, transaction, handle, OpenMode.ForWrite);
            ValidateCurrentEntity(entity, item);
            switch (entity)
            {
                case DBText text when entity.GetType() == typeof(DBText):
                    var originalDbTextBounds = TryGetExtents(text);
                    text.TextString = item.TranslatedText;
                    ApplyFontDecision(database, transaction, text, item.FontDecision);
                    FitSingleLineToOriginalBounds(text, originalDbTextBounds, text.Rotation, database);
                    break;
                case MText text when entity.GetType() == typeof(MText):
                    var originalMTextBounds = TryGetExtents(text);
                    text.Contents = NormalizeMTextFonts(
                        MTextProtector.Restore(item.TranslatedText, item.ProtectedSequences),
                        item.FontDecision);
                    FitMTextToOriginalBounds(text, originalMTextBounds);
                    ApplyFontDecision(database, transaction, text, item.FontDecision);
                    break;
                case AttributeReference text when entity.GetType() == typeof(AttributeReference):
                    var originalAttributeBounds = TryGetExtents(text);
                    text.TextString = item.TranslatedText;
                    ApplyFontDecision(database, transaction, text, item.FontDecision);
                    FitSingleLineToOriginalBounds(text, originalAttributeBounds, text.Rotation, database);
                    break;
                default:
                    throw new InvalidDataException($"Handle {handle} is not a supported exact entity type.");
            }
        }
        transaction.Commit();
    }

    private static Extents3d? TryGetExtents(Entity entity)
    {
        try { return entity.GeometricExtents; }
        catch (Autodesk.AutoCAD.Runtime.Exception) { return null; }
    }

    private static void FitSingleLineToOriginalBounds(Entity text, Extents3d? originalBounds,
        double rotation, Database database)
    {
        if (originalBounds is null)
            return;
        var currentBounds = TryGetExtents(text);
        if (currentBounds is null)
            return;

        var original = OrientedSize(originalBounds.Value, rotation);
        var current = OrientedSize(currentBounds.Value, rotation);
        if (original.Width <= 0 || original.Height <= 0 || current.Width <= 0 || current.Height <= 0)
            return;

        // A modest width change is harmless when the text still fits its row.  The
        // 15% guard is therefore a fallback cap, not a demand for exact old width.
        const double maximumRatio = 1.15;
        var heightScale = Math.Min(1, original.Height * maximumRatio / current.Height);
        if (heightScale is > 0 and < 1)
        {
            switch (text)
            {
                case AttributeReference attribute: attribute.Height *= heightScale; break;
                case DBText dbText: dbText.Height *= heightScale; break;
            }
            current = (current.Width * heightScale, current.Height * heightScale);
        }

        var widthScale = Math.Min(1, original.Width * maximumRatio / current.Width);
        if (widthScale is > 0 and < 1)
        {
            switch (text)
            {
                case AttributeReference attribute: attribute.WidthFactor *= widthScale; break;
                case DBText dbText: dbText.WidthFactor *= widthScale; break;
            }
        }

        switch (text)
        {
            case AttributeReference attribute: attribute.AdjustAlignment(database); break;
            case DBText dbText: dbText.AdjustAlignment(database); break;
        }
    }

    private static (double Width, double Height) OrientedSize(Extents3d bounds, double rotation)
    {
        var x = bounds.MaxPoint.X - bounds.MinPoint.X;
        var y = bounds.MaxPoint.Y - bounds.MinPoint.Y;
        var cosine = Math.Abs(Math.Cos(rotation));
        var sine = Math.Abs(Math.Sin(rotation));
        return (cosine * x + sine * y, sine * x + cosine * y);
    }

    private static void FitMTextToOriginalBounds(MText text, Extents3d? originalBounds)
    {
        if (originalBounds is null)
            return;
        var originalWidth = originalBounds.Value.MaxPoint.X - originalBounds.Value.MinPoint.X;
        var originalHeight = originalBounds.Value.MaxPoint.Y - originalBounds.Value.MinPoint.Y;
        var currentBounds = TryGetExtents(text);
        if (currentBounds is null)
            return;
        var currentWidth = currentBounds.Value.MaxPoint.X - currentBounds.Value.MinPoint.X;
        var currentHeight = currentBounds.Value.MaxPoint.Y - currentBounds.Value.MinPoint.Y;
        var widthScale = originalWidth > 0 ? originalWidth / currentWidth : 1;
        var heightScale = originalHeight > 0 ? originalHeight / currentHeight : 1;
        var scale = Math.Min(widthScale, heightScale);
        if (scale >= 0.98 || scale < 0.5)
            return;

        text.TextHeight *= scale;
    }

    private static void ApplyFontDecision(Database database, Transaction transaction, Entity entity,
        FontDecision? decision)
    {
        if (decision is null || string.IsNullOrWhiteSpace(decision.TargetFontFile) &&
            string.IsNullOrWhiteSpace(decision.TargetStyleName) && !decision.WidthFactor.HasValue)
            return;
        if (entity is MText && !decision.ApplyToMText)
            return;

        var sourceStyleId = entity switch
        {
            AttributeReference text => text.TextStyleId,
            MText text => text.TextStyleId,
            DBText text when entity.GetType() == typeof(DBText) => text.TextStyleId,
            _ => ObjectId.Null
        };
        if (sourceStyleId.IsNull)
            throw new InvalidDataException($"Entity {HandleText(entity)} has no text style.");

        if (!string.IsNullOrWhiteSpace(decision.TargetFontFile) ||
            !string.IsNullOrWhiteSpace(decision.TargetStyleName))
        {
            var targetStyle = GetOrCreateTargetStyle(database, transaction, sourceStyleId, decision);
            switch (entity)
            {
                case AttributeReference text: text.TextStyleId = targetStyle; text.AdjustAlignment(database); break;
                case MText text: text.TextStyleId = targetStyle; break;
                case DBText text when entity.GetType() == typeof(DBText): text.TextStyleId = targetStyle; text.AdjustAlignment(database); break;
            }
        }

        if (decision.WidthFactor is not > 0)
            return;
        switch (entity)
        {
            case AttributeReference text: text.WidthFactor = decision.WidthFactor.Value; break;
            case DBText text when entity.GetType() == typeof(DBText): text.WidthFactor = decision.WidthFactor.Value; break;
        }
    }

    private static ObjectId GetOrCreateTargetStyle(Database database, Transaction transaction,
        ObjectId sourceStyleId, FontDecision decision)
    {
        var source = (TextStyleTableRecord)transaction.GetObject(sourceStyleId, OpenMode.ForRead);
        var requestedName = decision.TargetStyleName;
        var name = string.IsNullOrWhiteSpace(requestedName)
            ? $"TR_ZH_{SanitizeStyleName(source.Name)}_{Sha256Text(
                $"{source.Handle}:{decision.TargetFontFile}:{decision.TargetBigFontFile}")[..8]}"
            : SanitizeStyleName(requestedName);
        var table = (TextStyleTable)transaction.GetObject(database.TextStyleTableId, OpenMode.ForRead);
        if (table.Has(name))
            return table[name];

        table.UpgradeOpen();
        var clone = new TextStyleTableRecord { Name = name };
        clone.FileName = RequireFontFile(decision.TargetFontFile);
        clone.BigFontFileName = decision.TargetBigFontFile ?? string.Empty;
        clone.XScale = source.XScale;
        clone.ObliquingAngle = source.ObliquingAngle;
        clone.TextSize = source.TextSize;
        clone.IsVertical = source.IsVertical;
        clone.IsShapeFile = false;
        var id = table.Add(clone);
        transaction.AddNewlyCreatedDBObject(clone, true);
        return id;
    }

    private static string NormalizeMTextFonts(string contents, FontDecision? decision)
    {
        if (decision is null || !decision.ApplyToMText || string.IsNullOrWhiteSpace(decision.TargetFontFile))
            return contents;
        var font = Path.GetFileNameWithoutExtension(decision.TargetFontFile);
        return Regex.Replace(contents, @"\\f[^|;]+(?<attrs>(?:\|[^;]*)?);",
            match => $"\\f{font}{match.Groups["attrs"].Value};",
            RegexOptions.CultureInvariant);
    }

    private static string RequireFontFile(string? fontFile)
    {
        if (string.IsNullOrWhiteSpace(fontFile))
            throw new InvalidDataException("Font decision requires target_font_file when creating a style.");
        if (fontFile.Contains(Path.DirectorySeparatorChar) || fontFile.Contains(Path.AltDirectorySeparatorChar))
            throw new InvalidDataException("Font decision must use a font file name, not an arbitrary path.");
        var trimmed = fontFile.Trim();
        if (string.Equals(Path.GetExtension(trimmed), ".ttc", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException(
                "TTC font collections are not reliable for CAD writeback; use a single-face TTF/OTF font.");
        return trimmed;
    }

    private static string SanitizeStyleName(string value)
    {
        var output = new string(value.Select(character => char.IsLetterOrDigit(character) || character == '_' ? character : '_').ToArray());
        return string.IsNullOrWhiteSpace(output) ? "TR_ZH_STYLE" : output[..Math.Min(output.Length, 240)];
    }

    private static void VerifyChangedText(Database database, IReadOnlyDictionary<string, TranslationItem> translations)
    {
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        foreach (var (handle, item) in translations)
        {
            var entity = GetEntity(database, transaction, handle, OpenMode.ForRead);
            var expected = item.EntityType == nameof(MText)
                ? NormalizeMTextFonts(
                    MTextProtector.Restore(item.TranslatedText, item.ProtectedSequences),
                    item.FontDecision)
                : item.TranslatedText;
            var actual = ReadRawText(entity);
            // AutoCAD rewrites MText formatting codes on save: it drops
            // redundant ones ("\pxsm0.7;", a font switch before "。") and
            // recases font names (simhei -> SimHei). Formatting is therefore
            // compared as AutoCAD renders it: the plain text, read the same
            // way from the saved entity and from the expected contents.
            if (entity is MText saved)
            {
                using var probe = new MText();
                probe.Contents = expected;
                expected = probe.Text;
                actual = saved.Text;
            }
            if (!string.Equals(actual, expected, StringComparison.Ordinal))
                throw new InvalidDataException(
                    $"Saved text verification failed for handle {handle}: expected \"{expected}\", saved \"{actual}\".");
        }
        transaction.Commit();
    }

    private static Entity GetEntity(Database database, Transaction transaction, string handle, OpenMode mode)
    {
        var value = HandleValue(handle);
        var id = database.GetObjectId(false, new Handle(value), 0);
        if (id.IsNull || transaction.GetObject(id, mode, false) is not Entity entity)
            throw new InvalidDataException($"Handle {handle} does not identify an entity.");
        return entity;
    }

    private static void ValidateCurrentEntity(Entity entity, TranslationItem item)
    {
        if (!string.Equals(entity.GetType().Name, item.EntityType, StringComparison.Ordinal) ||
            entity.GetType().Name is not (nameof(DBText) or nameof(MText) or nameof(AttributeReference)))
            throw new InvalidDataException($"Entity type changed for handle {item.Handle}.");
        var raw = ReadRawText(entity);
        var expectedSource = item.EntityType == nameof(MText)
            ? MTextProtector.Restore(item.SourceText, item.ProtectedSequences)
            : item.SourceText;
        if (!string.Equals(raw, expectedSource, StringComparison.Ordinal) ||
            !string.Equals(Sha256Text(raw), item.SourceHash, StringComparison.Ordinal))
            throw new InvalidDataException($"Source text changed for handle {item.Handle}.");
    }

    private static string ReadRawText(Entity entity) => entity switch
    {
        DBText text when entity.GetType() == typeof(DBText) => text.TextString,
        MText text when entity.GetType() == typeof(MText) => text.Contents,
        AttributeReference text when entity.GetType() == typeof(AttributeReference) => text.TextString,
        _ => throw new InvalidDataException("Unsupported entity type.")
    };

    private static IReadOnlyDictionary<string, TranslationItem> ValidateTranslations(
        ExportDocument export, TranslationItem[] translations)
    {
        ArgumentNullException.ThrowIfNull(translations);
        var exported = export.Items.ToDictionary(item => item.Handle, StringComparer.Ordinal);
        var result = new Dictionary<string, TranslationItem>(StringComparer.Ordinal);
        foreach (var item in translations)
        {
            ValidateHandle(item.Handle);
            if (!result.TryAdd(item.Handle, item))
                throw new InvalidDataException($"Duplicate translation handle {item.Handle}.");
            if (!SupportedTypes.Contains(item.EntityType))
                throw new InvalidDataException($"Unsupported entity_type {item.EntityType}.");
            if (!exported.TryGetValue(item.Handle, out var original))
                throw new InvalidDataException($"Translation handle {item.Handle} was not exported.");
            if (item.EntityType != original.EntityType || item.SourceText != original.SourceText ||
                item.SourceHash != original.SourceHash || !SequencesEqual(item.ProtectedSequences, original.ProtectedSequences))
                throw new InvalidDataException($"Translation source contract differs for handle {item.Handle}.");
            if (string.IsNullOrWhiteSpace(item.TranslatedText))
                throw new InvalidDataException($"Empty translated_text for handle {item.Handle}.");
            if (item.EntityType == nameof(MText))
                MTextProtector.Validate(item.TranslatedText, item.ProtectedSequences);
            else if (item.ProtectedSequences.Length != 0)
                throw new InvalidDataException($"Non-MText handle {item.Handle} has protected sequences.");
        }
        if (!exported.Keys.ToHashSet(StringComparer.Ordinal).SetEquals(result.Keys))
            throw new InvalidDataException("Import translations must correspond exactly to all exported handles.");
        return result;
    }

    private static bool SequencesEqual(ProtectedSequence[] left, ProtectedSequence[] right) =>
        left.Length == right.Length && left.Zip(right).All(pair => pair.First == pair.Second);

    private static void ValidateExportDocument(ExportDocument document, string source)
    {
        ValidateHeader(document.SchemaVersion, document.Operation, Contract.ExportResultOperation);
        if (!PathEquals(document.SourceDwg, source))
            throw new InvalidDataException("Export document source_dwg differs from import source_dwg.");
        var handles = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in document.Items ?? throw new InvalidDataException("Missing export items."))
        {
            ValidateHandle(item.Handle);
            if (!handles.Add(item.Handle))
                throw new InvalidDataException($"Duplicate exported handle {item.Handle}.");
            if (!SupportedTypes.Contains(item.EntityType))
                throw new InvalidDataException($"Unsupported exported entity_type {item.EntityType}.");
            if (item.SourceHash != Sha256Text(item.EntityType == nameof(MText)
                    ? MTextProtector.Restore(item.SourceText, item.ProtectedSequences) : item.SourceText))
                throw new InvalidDataException($"Invalid source hash for handle {item.Handle}.");
        }
    }

    private static void ValidateHeader(int version, string operation, string expectedOperation)
    {
        if (version != Contract.SchemaVersion)
            throw new InvalidDataException($"Unsupported schema_version {version}.");
        if (!string.Equals(operation, expectedOperation, StringComparison.Ordinal))
            throw new InvalidDataException($"Expected operation {expectedOperation}.");
    }

    private static string ValidateDwgSource(string value)
    {
        var path = Path.GetFullPath(RequireText(value, "source_dwg"));
        RequireDwgExtension(path, "source_dwg");
        if (!File.Exists(path))
            throw new FileNotFoundException("Source DWG does not exist.", path);
        return path;
    }

    private static string ValidateDestination(string source, string value, bool overwrite)
    {
        var destination = Path.GetFullPath(RequireText(value, "destination_dwg"));
        RequireDwgExtension(destination, "destination_dwg");
        if (PathEquals(source, destination))
            throw new InvalidDataException("source_dwg and destination_dwg must be distinct.");
        EnsureOutputAvailable(destination, overwrite, "destination_dwg");
        return destination;
    }

    private static void RequireDwgExtension(string path, string field)
    {
        if (!string.Equals(Path.GetExtension(path), ".dwg", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{field} must have a .dwg extension.");
    }

    private static void EnsureOutputAvailable(string path, bool overwrite, string field)
    {
        if (File.Exists(path) && !overwrite)
            throw new IOException($"{field} already exists and overwrite is false.");
    }

    private static void RequireDistinctPaths(params (string Field, string Path)[] paths)
    {
        for (var left = 0; left < paths.Length; left++)
        for (var right = left + 1; right < paths.Length; right++)
            if (PathEquals(paths[left].Path, paths[right].Path))
                throw new InvalidDataException($"{paths[left].Field} and {paths[right].Field} must be distinct.");
    }

    // Blank text (an empty block attribute) has nothing to translate, and an
    // import cannot carry an empty translation for it, so it is not exported.
    private static bool ContainsUnsupportedTextControls(string text) =>
        string.IsNullOrWhiteSpace(text) || text.Contains("%%", StringComparison.Ordinal) || text.Contains(@"\U+", StringComparison.OrdinalIgnoreCase);

    private static void ValidateLayerLists(string[] allow, string[] deny)
    {
        ArgumentNullException.ThrowIfNull(allow);
        ArgumentNullException.ThrowIfNull(deny);
        if (allow.Any(string.IsNullOrWhiteSpace) || deny.Any(string.IsNullOrWhiteSpace))
            throw new InvalidDataException("Layer names must be non-empty.");
        if (allow.Distinct(StringComparer.OrdinalIgnoreCase).Count() != allow.Length ||
            deny.Distinct(StringComparer.OrdinalIgnoreCase).Count() != deny.Length)
            throw new InvalidDataException("Layer lists contain duplicates.");
    }

    private static void ValidateHandle(string handle)
    {
        if (string.IsNullOrEmpty(handle) || handle.Any(character =>
                !char.IsDigit(character) && (character < 'A' || character > 'F')))
            throw new InvalidDataException($"Invalid uppercase hexadecimal handle {handle}.");
        _ = HandleValue(handle);
    }

    private static long HandleValue(string handle)
    {
        if (!long.TryParse(handle, NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out var value) || value <= 0)
            throw new InvalidDataException($"Invalid handle {handle}.");
        return value;
    }

    private static string HandleText(DBObject value) => value.Handle.ToString().ToUpperInvariant();
    private static bool PathEquals(string left, string right) =>
        string.Equals(Path.GetFullPath(left).TrimEnd(Path.DirectorySeparatorChar),
            Path.GetFullPath(right).TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase);
    private static string RequireText(string? value, string field) =>
        !string.IsNullOrWhiteSpace(value) ? value : throw new InvalidDataException($"Missing {field}.");

    private static T ReadJson<T>(string path)
    {
        var json = File.ReadAllText(path, Encoding.UTF8);
        return JsonSerializer.Deserialize<T>(json, JsonOptions)
            ?? throw new InvalidDataException($"JSON file {path} is empty.");
    }

    private static void WriteJsonAtomic<T>(string path, T value, bool overwrite)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temporary = Path.Combine(Path.GetDirectoryName(path)!, $".{Path.GetFileName(path)}.{Guid.NewGuid():N}.tmp");
        try
        {
            File.WriteAllText(temporary, Serialize(value), new UTF8Encoding(false));
            File.Move(temporary, path, overwrite);
        }
        finally
        {
            DeleteIfExists(temporary);
        }
    }

    private static void PublishOutputs(string temporaryDwg, string destination, string temporaryResult, string resultJson)
    {
        string? destinationBackup = null;
        string? resultBackup = null;
        var destinationMovedAside = false;
        var resultMovedAside = false;
        var destinationPublished = false;
        var resultPublished = false;
        var publishedBoth = false;
        try
        {
            if (File.Exists(destination))
            {
                destinationBackup = UniqueSibling(destination, ".bak");
                File.Move(destination, destinationBackup);
                destinationMovedAside = true;
            }
            if (File.Exists(resultJson))
            {
                resultBackup = UniqueSibling(resultJson, ".bak");
                File.Move(resultJson, resultBackup);
                resultMovedAside = true;
            }
            File.Move(temporaryDwg, destination);
            destinationPublished = true;
            File.Move(temporaryResult, resultJson);
            resultPublished = true;
            publishedBoth = true;
        }
        catch
        {
            if (!publishedBoth)
            {
                if (destinationPublished)
                    DeleteIfExists(destination);
                if (resultPublished)
                    DeleteIfExists(resultJson);
                if (destinationMovedAside)
                    File.Move(destinationBackup!, destination);
                if (resultMovedAside)
                    File.Move(resultBackup!, resultJson);
            }
            throw;
        }
        finally
        {
            DeleteIfExists(temporaryDwg);
            DeleteIfExists(temporaryResult);
            if (publishedBoth)
            {
                DeleteIfExists(destinationBackup);
                DeleteIfExists(resultBackup);
            }
        }
    }

    private static string UniqueSibling(string path, string suffix) =>
        Path.Combine(Path.GetDirectoryName(path)!, $".{Path.GetFileName(path)}.{Guid.NewGuid():N}{suffix}");

    private static string Serialize<T>(T value) => JsonSerializer.Serialize(value, JsonOptions) + "\n";
    private static string Sha256File(string path) => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant();
    private static string Sha256Text(string text) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    private static void DeleteIfExists(string? path)
    {
        if (!string.IsNullOrEmpty(path) && File.Exists(path))
            File.Delete(path);
    }

    private sealed class WorkingDatabaseScope : IDisposable
    {
        private readonly Database? _previous;

        public WorkingDatabaseScope(Database database)
        {
            _previous = HostApplicationServices.WorkingDatabase;
            HostApplicationServices.WorkingDatabase = database;
        }

        public void Dispose()
        {
            HostApplicationServices.WorkingDatabase = _previous;
        }
    }

    private static void RequireSameStructure(StructuralSnapshot expected, StructuralSnapshot actual, string message)
    {
        if (expected != actual)
            throw new InvalidDataException(message);
    }

}
