# General Rules for English-to-Chinese Translation

## 1. Scope of Application

These rules apply to the translation of engineering, contract, technical, business, and general office documents from English to Chinese. The objectives are:

- Accurately convey the meaning of the original text;
- Preserve numbers, units, identifiers, and formatting information;
- Standardize translations of proper nouns and terminology;
- Avoid engineering, legal, or layout errors caused by translation.

## 2. General Principles

1. **Identify first, then translate**: Distinguish between ordinary text, proper nouns, numerical units, identifiers, codes, and format control content.
2. **Prioritize authoritative translations when available**: Base decisions on contracts, drawings, business licenses, project documents, standard texts, or official materials.
3. **Handle cautiously when no authoritative translation exists**: Do not arbitrarily translate company names, brands, projects, or personal names literally.
4. **No loss of original information**: Numbers, units, ranges, identifiers, symbols, and format control codes must be fully preserved.
5. **Consistent terminology throughout**: The same name, term, or acronym shall use only one primary translation within a document.
6. **Natural Chinese expression**: Adjust word order, punctuation, and sentence structure without altering technical meaning.

### 2.1 Classification of Translation Objects

Before translating, categorize content into four types:

| Category | Handling Method |
|---|---|
| Ordinary Language | Translate into natural and accurate Chinese |
| Proper Nouns with Authoritative Translations | Use the official project or authorized translation |
| Terms appearing for the first time; Names without unified translations | Retain the original English after the Chinese translation; use the confirmed translation consistently thereafter |
| Protected Items such as Identifiers, Codes, Formulas, Paths, and Control Codes | Do not translate at all; protect using placeholders and restore exactly as is |

Do not mistake "retaining English" for "omission," nor arbitrarily translate company names, brand names, model numbers, or codes literally.

## 3. Proper Noun Rules

| Content | Processing Rules | Examples |
|---|---|---|
| Countries, Cities, Institutions | Use the official name if an official Chinese name exists | `Pakistan` → `巴基斯坦`；`World Bank` → `世界银行` |
| Place names without a unified translation | Transliteration or established translation; retain English on first occurrence | `Sukkur` → `苏库尔（Sukkur）` |
| Personal Names | Usually transliterated, not translated literally by meaning | `Muhammad Ali` → `穆罕默德·阿里` |
| Company Names | Use the formal name if a formal Chinese name exists; otherwise retain English or transliterate, do not arbitrarily translate by meaning | `China Civil Engineering Construction Corporation` → `中国土木工程集团有限公司`；`Minconsult` → `Minconsult` |
| Projects, Roads, Bridges | Use the formal translation if available; otherwise use Chinese description plus original English name | `N-55 Highway` → `N-55公路（N-55 Highway）` |
| Brands, Software, Products | Prioritize official Chinese name; otherwise retain original text | `AutoCAD`、`Microsoft`、`Qwen` |
| Standards and Specifications | Retain standard numbers and English codes | `ISO 9001`、`ASTM A36`、`BS EN 1992` |
| Abbreviations | Retain English abbreviations; supplement with full Chinese name on first occurrence | `ADB` → `亚洲开发银行（ADB）` |

The order for determining official names is: user-specified translations, current project contracts or drawings, institution/company official names, national or industry standards, project glossaries, authoritative transliterations, and finally model-generated temporary translations. Addresses, administrative regions, streets, postal codes, job titles, and legal entity suffixes shall also be determined according to the same principle.

It is recommended to use the following format on first occurrence:

```text
中国土木工程集团有限公司（China Civil Engineering Construction Corporation，简称 CCECC）
```

Subsequently, use the Chinese name or established abbreviation consistently.

For trademarks, registered company names, product series names, and software names without reliable Chinese names, retain the English and do not create Chinese names based on literal word meanings.

## 4. Number, Unit, and Symbol Rules

### 4.1 Content That Must Be Retained

- Numbers and decimal places: `58.780` must not be changed to `58,780`;
- Plus/minus signs: `+1.20`, `-1.44`;
- Numeric ranges and hyphens: `105+820–164+600`;
- Unit symbols: `m`, `km`, `MPa`, `kN`, `m²`, `m³`;
- Percent signs, angles, diameters, and tolerances: `5%`, `45°`, `Φ200`, `±0.002`;
- Scales, dimensions, and model numbers: `1:200`, `M20×100`;
- Dates, times, version numbers, contract numbers, drawing numbers, and station numbers;
- Scientific notation and formulas: `1.25×10⁶`, `x=10`.
- Currency codes, currency symbols, and amounts: `USD 1,250.50`, `PKR 2 million`;
- Superscripts, subscripts, fractions, range endpoints, and their binding to units.

### 4.2 Expression methods

- Chinese descriptions may be translated, but the numerical values themselves must not be altered:

  `All dimensions are in meters.`
  → `所有尺寸均以米为单位。`

- Engineering documents should prioritize retaining international unit symbols: `105+820 km`;
- General descriptive text may use Chinese units: `5米`, but consistency must be maintained within the same document;
- Do not treat `+` in station numbers as addition, nor split drawing numbers and identifiers.

Currency conversion, imperial-to-metric conversion, or other unit conversions shall only be performed when explicitly requested; when converting, the original value must be retained and the converted value separately annotated. The `million`, `billion`, thousands separators, and decimal precision in monetary amounts must be confirmed based on context and cannot be mechanically replaced.

### 4.3 Dates, Times, and Time Zones

- Ambiguous dates such as `03/04/2025` must be confirmed based on the document's region or context; do not arbitrarily determine the day/month order;
- `calendar days`, `working days`, `business days`, and `自然日/工作日` must be distinguished;
- Deadlines, start dates, inclusion of the current day, and duration days must not be omitted;
- Times, time zones, daylight saving time, quarters, and fiscal years must be retained;
- Dates may be displayed in Chinese format, but date values, time values, and time zones cannot be changed.

## 5. Content Not Translated or Default Protected

The following content shall be treated as immutable placeholders and restored exactly after translation:

- Numbers, units, formulas, variables, and mathematical symbols;
- Drawing numbers, contract numbers, project IDs, stationing, coordinates, and model types;
- URLs, email addresses, file names, file paths, and code;
- Standard numbers, English acronyms, brand names, and software names;
- CAD layer names, block names, attribute tags, font names, and MText control codes;
- HTML/XML tags, Markdown markers, and program placeholders.

Protected content must meet the following:

```text
原文保护项集合 == 译文保护项集合
原文数字集合 == 译文数字集合
原文单位集合 == 译文单位集合
原文编号集合 == 译文编号集合
```

Comparing sets alone is insufficient for acceptance. It is also necessary to check the occurrence count of each item, the order (or table position), and the binding relationships between number-unit, value-ID, and value-name pairs, to prevent errors where values are not lost but positions are swapped.

## 6. Terminology and Translation Management

Establish a glossary containing at least:

- Original English terms;
- Standard Chinese translations;
- Applicable fields;
- Prohibited translations;
- Format for first appearance;
- Source or confirmer;
- Effective version.

The terminology priority is as follows:

```text
合同/图纸明确译名
> 项目术语表
> 国家或行业标准译名
> 公司统一译名
> 通用词典译名
> 模型临时译法
```

## 7. Special Rules for Engineering and Contract Documents

- Defined terms such as `Employer`, `Engineer`, and `Contractor` shall be presented in both Chinese and English upon first appearance, and subsequently use Chinese uniformly;
- Legal obligations, conditions, exceptions, and negations shall not be weakened or omitted;
- For `shall`, `must`, `may`, and `should`, distinguish between "shall/must/may/should";
- Codes, standards, clause numbers, drawing references, and cross-references must retain their original numbering;
- Unit systems, precision, elevation datums, and coordinate systems shall not be changed;
- Company, project, road, and personnel names shall not change translation based on context.
- Rights, prerequisites, exceptions, and reservation clauses such as `is entitled to`, `subject to`, `unless otherwise stated`, `provided that`, and `without prejudice to` must be fully expressed;
- Conditions, responsible parties, approvals/consents/acceptances, notices, claims, time limits, and deemed events shall not be weakened;
- The handling of `and`, `or`, `and/or`, singular/plural forms, and passive voice should be context-dependent and not mechanically translated;
- Defined terms must maintain the same case correspondence throughout the document and cannot be arbitrarily retranslated due to ordinary context.

## 8. Chinese Expression and Typesetting Rules

- Use unified Chinese punctuation in the main text; keep half-width characters inside English abbreviations, codes, formulas, URLs, and file paths;
- Uniformly handle spaces, parentheses, quotation marks, colons, and dashes between Chinese and English text, numbers, and units;
- Do not change decimal points, thousands separators, negative signs, formula operators, or punctuation within paths;
- Select a formal, technical, business, or general explanatory style based on the document's purpose; do not add conclusions, causes, or liabilities not present in the original text;
- Professional terminology must not have qualifiers, degree words, negations, or conditional words deleted merely for fluency.

## 9. Document Structure and Context Rules

- Headers, footers, footnotes, endnotes, comments, revisions, text boxes, figure captions, table titles, legends, and axes are all subject to inspection;
- Merged cells, headers, unit columns, footnotes, and cross-references in tables must maintain their corresponding relationships;
- The display text of hyperlinks can be translated, but the actual address must not be changed;
- Table of contents, bookmarks, automatic numbering, fields, and cross-references must not fail due to translation;
- Visual line breaks in PDFs do not equate to natural paragraphs; a single sentence must not be incorrectly split into multiple paragraphs;
- Translation should use complete sentences and semantic segments as context; truncation causing errors in pronouns, terms, or conditional relationships is prohibited.

## 10. Anomalies and Uncertain Content

- When OCR garbled text, missing characters, broken lines, or scan damage cannot be reliably recovered, definitive translations must not be generated based on guesswork;
- Unconfirmed names, places, company names, abbreviations, numbers, or units should be listed in the review checklist;
- When the original text contains grammatical errors, contradictions, or suspected omissions, the original meaning should be preserved as much as possible and issues reported; silent rewriting is prohibited;
- Delivery is prohibited if placeholders, control codes, or protected items fail to restore;
- Distinguish between "intentionally retained English" and "untranslated ordinary text"; untranslated ordinary text must be reported as an omission.

## 11. Special Rules for CAD Documents

- Do not translate drawing numbers, station numbers, coordinates, dimensions, scales, or font control codes;
- Preserve entity handles, layers, blocks, layouts, and geometric relationships;
- Control sequences such as `\\P`, `\\p...;`, `\\H...;`, and `\\f...;` in MText must be protected;
- After translation, recalculate text boundaries; adjust only text height, width factor, and alignment if necessary;
- 15% is a warning threshold, not the sole condition prohibiting write-back: first determine if the translation remains within the original line, cell, or allowed area, then check for overflow, overlap with other text, or graphic elements;
- If it remains within the allowed area, even if the width growth exceeds 15%, the original size may be maintained and the situation recorded;
- If overflow or overlap occurs, try verified fonts, text heights, width factors, alignments, and line breaks in sequence; after adjustment, write-back is still mandatory, and the adjustment method and review status must be recorded;
- DWG files do not embed fonts; before delivery, they must be actually opened and checked in the target AutoCAD version.

## 12. Post-Translation Acceptance

### Content Acceptance

- Proper nouns are consistent with the glossary;
- Company names, personal names, place names, and project names have not been arbitrarily paraphrased;
- Numbers, units, identifiers, and symbols are complete and consistent;
- No question marks, boxes, replacement characters, or garbled text;
- No omissions, duplications, mistranslations, or untranslated ordinary text.
- Binding relationships for amounts, dates, times, time zones, number-unit pairs, and number-identifier pairs are correct;
- Obligation strength, conditions, exceptions, responsible parties, and time limits are consistent with the original text.

### Format Acceptance

- Structure of headings, paragraphs, tables, headers/footers, and lists is maintained;
- CAD text has not shifted position, does not overlap wireframes, and does not exceed cells;
- Translations are actually opened and checked in the target software, rather than performing only source file structure checks;
- Output files are saved as new files; original files are not overwritten.

### Minimum Requirements for Automated Acceptance

- Source and target paragraphs, table rows, list items, and CAD text objects correspond one-to-one;
- Occurrence counts, order/position, and binding relationships of protected items are consistent;
- No question marks, boxes, replacement characters, garbled text, or missing font glyphs;
- Both structural validation and actual opening checks in the target software have passed;
- All content that cannot be automatically determined is entered into the manual review list.

## 13. Recommended Processing Workflow

```text
提取文本
→ 识别专有名词、数字单位和保护项
→ 应用术语表
→ 翻译普通语言
→ 恢复保护项
→ 数字/单位/编号一致性检查
→ 格式与版面检查
→ 目标软件实际打开验证
→ 生成新文件并记录验收结果
```
