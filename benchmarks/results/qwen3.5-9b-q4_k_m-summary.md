# 本地 Qwen 百条工程翻译基准结果

相似度仅为与 candidate 参考译文的字符级参考指标，不是翻译质量得分或准确率。

- 总记录：100；已有结果：100；成功：35；失败：65
- 首次成功：35；重试后成功：0；最终失败：65
- 总耗时：1098.126 s；平均：10.981 s；中位数：0.011 s；P95：43.837 s

## 结构检查失败数量

- json: 0
- id: 0
- protected_literal_mismatch: 0
- required_term_missing: 0

## 方向统计

|方向|总数|成功|失败|
|---|---:|---:|---:|
|en→zh-CN|50|0|50|
|zh-CN→en|50|35|15|

## 类别统计

|类别|总数|成功|失败|
|---|---:|---:|---:|
|concrete_rebar|10|5|5|
|construction_quality_safety|10|5|5|
|contract_submittal_rfi|10|1|9|
|correspondence|10|0|10|
|earth_foundation|10|4|6|
|electrical_scada_ventilation|10|0|10|
|mtbm|10|5|5|
|road_bridge|10|5|5|
|sewer_manhole|10|5|5|
|survey|10|5|5|

## 字符相似度参考分布

|区间|条数|
|---|---:|
|<0.5|5|
|0.5-0.74|14|
|0.75-0.89|7|
|0.90-1.00|9|

## 需人工复核记录

所有记录均为 candidate 基准，`needs_manual_review`全部为 true；以下为最终失败记录：
- eng-zh-028: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-037: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-038: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-039: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-040: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-041: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-042: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-043: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-044: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-045: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-046: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-047: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-048: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-049: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-zh-050: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-001: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-002: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-003: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-004: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-005: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-006: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-007: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-008: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-009: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-010: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-011: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-012: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-013: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-014: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-015: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-016: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-017: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-018: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-019: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-020: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-021: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-022: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-023: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-024: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-025: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-026: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-027: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-028: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-029: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-030: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-031: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-032: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-033: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-034: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-035: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-036: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-037: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-038: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-039: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-040: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-041: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-042: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-043: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-044: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-045: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-046: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-047: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-048: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-049: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503
- eng-en-050: Server error '503 Service Unavailable' for url 'http://127.0.0.1:8088/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503

## 运行边界

参考译文未发送给模型，未使用 grammar、--json-schema 或 response_format；失败记录保留原文语义，不自动改写 candidate 或 review_status。
