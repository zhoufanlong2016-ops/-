# 阶段0A：本地 Qwen 运行与性能基线

记录日期：2026-09-02
对应技术方案：V2.2（本地模型实测基线修订）

## 设备和软件环境

- Windows 10 x64
- Intel Core i7-1165G7，8逻辑线程；约16 GB内存；Intel Iris Xe，无独立显卡
- 项目 Python：`D:\Program Files\Python\python.exe`
- llama.cpp目录：`D:\Program Files\llama.cpp`
- 本轮未启动常驻模型服务；基准使用独立 `llama-cli` 进程

## 版本和模型校验

```text
llama-cli/llama-server:
version: 0.1.0-dev (build 10442, commit 9b0a2ce85)
built with Clang 20.1.8 for Windows x86_64
```

- 模型：`D:\Program Files\Qwen\Qwen3.5-9B-Q4_K_M.gguf`
- 文件大小：5,680,522,464 字节
- `certutil -hashfile <model> SHA256`：`03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8`
- `D:\下载`中的同名副本未复制、移动或删除；其哈希未写入本基线

## 实际基准参数

```text
-ngl 0
-t 8 -tb 8
-c 4096
-b 256 -ub 128
--temp 0 --seed 42
--reasoning off --reasoning-budget 0
```

最小加载命令：

```powershell
& "D:\Program Files\llama.cpp\llama-cli.exe" -m "D:\Program Files\Qwen\Qwen3.5-9B-Q4_K_M.gguf" -ngl 0 -t 8 -tb 8 -c 4096 -b 256 -ub 128 -n 32 --temp 0 --seed 42 --reasoning off --no-conversation --single-turn --no-display-prompt --perf -p "Return exactly OK and nothing else."
```

最小加载测试成功，退出码0，总耗时17.568秒；提示处理7.2 token/s，生成3.5 token/s。批量复测中生成速度约2.5–2.6 token/s，提示处理约5.9–8.1 token/s。`llama-cli`未单独输出模型加载阶段耗时；各项墙钟时间包含加载、提示处理和生成。

## 通过项目

- CPU-only、`-ngl 0`、4096上下文下模型可重复加载运行。
- 无 grammar 的 JSON 对象：Assistant内容经 Python `json.loads`成功。
- 无 grammar 的 JSON 数组：Assistant内容经 Python `json.loads`成功。
- 无 grammar 的 JSON+两个占位符：经 Python `json.loads`成功；`⟦PH_0001⟧`和`⟦PH_0002⟧`各出现1次。
- 明确源/目标语言的中文→英文普通、术语、数字/单位/桩号测试通过。
- 明确源/目标语言的英文→中文普通测试通过。
- 明确源/目标语言的中文→英文双占位符测试通过，两个占位符均原样保留且各1次。
- 相同提示连续执行两次，输出结构和内容一致。

## grammar 故障诊断

使用当前版本帮助确认的 `--json-schema-file`，并将最小 Schema 写入系统临时目录，排除了 PowerShell 内联引号转义影响。以下三类均复现：

1. 只有一个字符串字段的对象；
2. `id`和`translation`两个字符串字段的对象；
3. 对象数组。

原始错误：

```text
Error: Failed to initialize samplers: Unexpected empty grammar stack after accepting piece: <|im_start|> (248045)
[ Prompt: 0.0 t/s | Generation: 0.0 t/s ]
```

错误发生在模型生成前的 sampler/grammar 初始化阶段，不是模型生成了非法 JSON。无 grammar 时同类 JSON 可被 `json.loads`解析，因此当前结论是 llama.cpp grammar/chat-template兼容路径问题，不能归因于模型能力。

## 正式 provider 校验和失败降级

正式本地运行采用常驻 `llama-server`，不逐单元反复启动 `llama-cli`。本地Qwen不启用 `--grammar`或`--json-schema`，而采用：

```text
严格JSON提示词
→ json.loads
→ Pydantic Schema验证
→ unit ID、数量、占位符、哈希确定性校验
→ 失败重试
→ 最终失败保留原文并标记
```

云端provider如原生支持 `response_format`/JSON Schema可使用其原生约束，但仍执行相同应用层校验。grammar仅作为未来兼容llama.cpp版本后的可选增强，不是当前本地provider启用硬条件。

所有提示必须明确写出源语言和目标语言。

## 尚未完成

- 技术方案要求的不少于100条真实工程短句双向基准尚未建立和执行。
- 五种格式黄金样本、Office 365 COM及AutoCAD 2025 .NET 8最小插件验证不属于本轮，仍待后续阶段0工作。
