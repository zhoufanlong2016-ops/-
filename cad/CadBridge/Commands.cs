using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;

[assembly: CommandClass(typeof(CadBridge.Commands))]

namespace CadBridge;

public sealed class Commands : IExtensionApplication
{
    public void Initialize() { }
    public void Terminate() { }

    [CommandMethod("TR_EXPORT", CommandFlags.Modal | CommandFlags.NoBlockEditor)]
    public static void Export()
    {
        Run("export", DwgBridge.Export);
    }

    [CommandMethod("TR_IMPORT", CommandFlags.Modal | CommandFlags.NoBlockEditor)]
    public static void Import()
    {
        Run("import", DwgBridge.Import);
    }

    private static void Run(string operation, Action<string> action)
    {
        var editor = Application.DocumentManager.MdiActiveDocument.Editor;
        var options = new PromptStringOptions($"\nCadBridge {operation} task JSON path: ")
        {
            AllowSpaces = true
        };
        var prompt = editor.GetString(options);
        if (prompt.Status != PromptStatus.OK)
            return;
        try
        {
            action(Path.GetFullPath(prompt.StringResult.Trim().Trim('"')));
            editor.WriteMessage($"\nCadBridge {operation} completed.");
        }
        catch (System.Exception exception)
        {
            editor.WriteMessage($"\nCadBridge {operation} failed: {exception}");
        }
    }
}
