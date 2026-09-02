# document-translator

## 阶段 1A

当前已实现：

- 使用 Pydantic v2 定义统一翻译数据模型：`DocumentFormat`、`UnitStatus`、`DocumentLocation`、`TranslationUnit` 和 `TranslationResult`。
- 支持 `md`、`docx`、`pptx`、`pdf`、`dwg` 五种格式的严格校验。
- 基于规范化 JSON 和 SHA-256 生成稳定的 unit ID 与缓存键。
- 提供不修改译文的占位符内容、大小写和出现次数校验。
- 提供模型 JSON 序列化/反序列化及基础确定性校验。

本阶段不包含任何文档格式适配器、模型调用或网络 API。
