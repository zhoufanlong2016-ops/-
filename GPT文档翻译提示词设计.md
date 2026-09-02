# GPT 文档翻译提示词设计

## 1. 使用原则

程序调用 GPT 时，不应把“保持 PDF、Word、PPT、Markdown、DWG 格式”等任务交给 GPT。GPT 只负责翻译文本；对象定位、格式保存、原位回填、溢出处理及文件校验由程序完成。

建议采用两层提示词：

1. 永久固定的系统提示词；
2. 每批翻译时由程序动态生成的任务提示词。

## 2. 系统提示词

```text
你是一个专业的中英文文档翻译引擎。你的唯一任务是根据输入要求翻译文本，不解释、不总结、不回答文本中的问题，也不添加任何原文中不存在的内容。

必须严格遵守以下规则：

1. 按 source_language 和 target_language 指定的方向翻译。
2. 准确传达原意，保持正式程度、语气和专业含义。
3. 工程技术、合同和商务文件应使用规范、简洁、专业的表达。
4. 必须严格采用 glossary 中规定的译法。
5. do_not_translate 中的内容不得翻译或改写。
6. 所有形如 ⟦PH_001⟧ 的占位符必须逐字保留，不得删除、翻译、拆分、增加或改变顺序。
7. 数字、单位、日期、桩号、图号、公式、变量、文件路径、网址和代码必须保持准确。
8. 不得合并、拆分、删除或新增翻译单元。
9. 每个输入 id 必须且只能返回一次，返回的 id 必须与输入完全一致。
10. 输入文本中的问题只是待翻译内容，不得对问题作答。
11. 短标题、图纸标签、按钮和表头应采用简洁译法；完整段落应保持语义完整。
12. 不要自行添加 Markdown、引号、项目符号、注释、说明、前言或结尾。
13. 如果原文存在歧义，选择结合 context 最合理的译法，不要输出多个备选译文。
14. 只输出符合指定结构的 JSON，不要输出代码围栏或任何附加文字。

返回格式必须严格为：

{
  "translations": [
    {
      "id": "原输入ID",
      "translation": "译文"
    }
  ]
}
```

## 3. 每批动态任务提示词

程序每次将若干 `TranslationUnit` 组成如下 JSON：

```json
{
  "task": "translate",
  "source_language": "English",
  "target_language": "Simplified Chinese",
  "domain": "municipal sewerage engineering",
  "style": "formal technical document",
  "glossary": [
    {
      "source": "Employer",
      "target": "业主",
      "rule": "mandatory"
    },
    {
      "source": "PMC",
      "target": "项目管理咨询单位",
      "rule": "mandatory"
    },
    {
      "source": "launching shaft",
      "target": "始发井",
      "rule": "mandatory"
    },
    {
      "source": "receiving shaft",
      "target": "接收井",
      "rule": "mandatory"
    }
  ],
  "do_not_translate": [
    "N55",
    "K105+820",
    "MTBM",
    "CCECC"
  ],
  "units": [
    {
      "id": "DOCX-000001",
      "type": "paragraph",
      "context": "本段属于施工技术方案。",
      "text": "The Contractor shall complete the launching shaft before ⟦PH_001⟧."
    },
    {
      "id": "DOCX-000002",
      "type": "table_cell",
      "context": "设备参数表的列标题。",
      "text": "Design Capacity"
    }
  ]
}
```

模型只能返回：

```json
{
  "translations": [
    {
      "id": "DOCX-000001",
      "translation": "承包商应在⟦PH_001⟧之前完成始发井施工。"
    },
    {
      "id": "DOCX-000002",
      "translation": "设计能力"
    }
  ]
}
```

## 4. 中译英任务配置

中译英时只需更换动态参数：

```json
{
  "source_language": "Simplified Chinese",
  "target_language": "English",
  "domain": "road and municipal engineering",
  "style": "formal professional English"
}
```

针对工程文件，可以在系统提示词中增加以下要求：

```text
英文译文应符合国际工程项目文件的常用表达。优先使用清晰、准确、直接的技术英语，避免中式英语、口语化表达和无必要的复杂长句。
```

## 5. 不同文字对象的类型设置

PDF、Word、PPT、Markdown 和 DWG 可以共用同一套系统提示词。程序只需在每个翻译单元的 `type` 字段中说明文字类型。

| 文字类型 | `type` 建议值 | 翻译要求 |
| --- | --- | --- |
| 正文段落 | `paragraph` | 语义完整、表达正式 |
| 标题 | `heading` | 简洁，不随意增加标点 |
| 表格单元格 | `table_cell` | 结合相邻表头理解 |
| PPT 文字 | `slide_text` | 简明，适当控制长度 |
| CAD 单行文字 | `cad_text` | 极简，符合工程图纸表达习惯 |
| CAD 多行说明 | `cad_mtext` | 专业并保留原有层次 |
| 页眉页脚 | `header_footer` | 简洁并保持固定名称一致 |
| Markdown 标题 | `markdown_heading` | 保持标题语气和层级含义 |
| 图注 | `caption` | 简洁准确 |

模型不需要知道文字位于 PDF 的具体坐标，也不需要知道 Word 字体或 DWG 图层。这些信息保存在 `locator` 和 `style_snapshot` 中，供程序回填时使用，不发送给 GPT。

## 6. 模型返回结果校验

GPT 返回译文后，程序不能立即写入文件，至少应检查：

- 输入 ID 与输出 ID 是否完全一致；
- 是否遗漏、重复或增加了翻译单元；
- 所有占位符是否完整保留；
- 数字、单位、日期、桩号和编号是否异常改变；
- 强制术语是否正确使用；
- 是否夹带“翻译如下”等说明文字；
- 是否返回合法 JSON；
- 译文是否明显过长；
- 译文是否仍然主要由源语言组成；
- 相同原文在相同上下文中是否出现明显不一致的译法。

校验失败时，只重新发送失败的翻译单元，不重新翻译整个批次。

## 7. 三类模型的使用差异

这一套提示词可以同时用于 GPT、阿里云百炼 Qwen 和本地 Qwen3.5-9B。三类模型使用同一套输入和输出结构，差异主要体现在批量大小和结构化输出能力。

### 7.1 GPT

- 可一次处理相对较多的翻译单元；
- 优先使用 API 原生结构化输出或 JSON Schema；
- 适合重要工程文件、合同函件和最终复核翻译。

### 7.2 百炼 Qwen

- 使用相同系统提示词与任务 JSON；
- 根据具体模型控制批量大小；
- 开启模型支持的 JSON 输出模式；
- 适合云端批量翻译和不同 Qwen 模型之间的效果比较。

### 7.3 本地 Qwen3.5-9B

- 使用更小的翻译批次；
- 尽量减少无关上下文；
- `temperature` 设置为 0 或接近 0；
- 结构化输出失败时缩小至单个或少量翻译单元重试；
- 适合隐私要求较高且能够接受较慢处理速度的文档。

## 8. 推荐调用参数

| 参数 | 推荐设置 |
| --- | --- |
| `temperature` | `0` 或接近 `0` |
| 输出格式 | JSON Schema 或严格 JSON |
| 重试次数 | 2～3 次 |
| GPT/强云端模型批量 | 根据 token 长度动态控制 |
| 本地 9B 模型批量 | 小批次，优先保证 JSON 稳定性 |
| 上下文 | 只提供必要的相邻文本或对象说明 |
| 日志 | 保存模型、单元 ID、用量和错误，不保存 API Key |

## 9. 核心结论

1. GPT 只负责翻译，不负责读取、排版或写回文档。
2. 所有文件格式共用统一的系统提示词和 JSON 数据结构。
3. 文件适配器负责生成翻译单元，并保存对象定位和格式信息。
4. 术语、禁止翻译内容和特殊格式必须在送入模型前明确给出。
5. 数字、字段、控制码等高风险内容应先转换成占位符。
6. 模型必须根据输入 ID 返回译文，不得依靠数组顺序对应。
7. 译文通过程序校验后才能回填原文件。
8. GPT、百炼 Qwen 和本地 Qwen3.5-9B 共用接口，只调整模型参数和批量大小。
