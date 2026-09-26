# document-translator

## 阶段 1A

当前已实现：

- 使用 Pydantic v2 定义统一翻译数据模型：`DocumentFormat`、`UnitStatus`、`DocumentLocation`、`TranslationUnit` 和 `TranslationResult`。
- 支持 `md`、`docx`、`pptx`、`pdf`、`dwg` 五种格式的严格校验。
- 基于规范化 JSON 和 SHA-256 生成稳定的 unit ID 与缓存键。
- 提供不修改译文的占位符内容、大小写和出现次数校验。
- 提供模型 JSON 序列化/反序列化及基础确定性校验。

本阶段不包含任何文档格式适配器、模型调用或网络 API。

## DWG 字体要求

注意：DWG 不嵌入字体；复制到其他电脑时，需要安装 `simhei.ttf`，否则仍可能由目标电脑产生字体替代。
# PDF backend

PDF translation is delegated to the installed BabelDOC worker (`pdf2zh_next`).
The legacy PyMuPDF block-rewrite module is diagnostic-only and is not used by
the `translate-pdf` command.

## 桌面界面

启动拖放式界面：

```powershell
.venv\Scripts\python.exe -m document_translator gui
```

界面支持 Markdown、DOCX、PPTX、XLSX 和 PDF 的自动格式识别，选择服务商/模型、源语言和目标语言后生成临时结果并打开预览，再由用户选择最终保存位置；CSV 或 XLSX 术语库可选。Windows 资源管理器中的文件可直接拖到窗口，翻译任务在后台执行，不覆盖源文件。DWG 会提示先通过 AutoCAD/CadBridge 导出文本任务，再执行导入闭环。
