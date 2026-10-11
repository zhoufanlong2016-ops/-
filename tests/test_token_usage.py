import json

import httpx

from document_translator.__main__ import main
from document_translator.providers import usage
from document_translator.providers import AnthropicConfig, AnthropicProvider

from test_anthropic_provider import make_unit


def test_usage_formats_mean_the_same() -> None:
    usage.reset()
    # Claude: cached input reported apart from input.
    usage.record_response({"usage": {"input_tokens": 900, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50, "output_tokens": 200}})
    # DeepSeek: the cache hits are part of prompt_tokens.
    usage.record_response({"usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 300}})
    # Qwen (OpenAI chat): cached_tokens in the details.
    usage.record_response({"usage": {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 100}, "completion_tokens": 50}})
    # OpenAI Responses API.
    usage.record_response({"usage": {"input_tokens": 300, "input_tokens_details": {"cached_tokens": 200}, "output_tokens": 30}})
    usage.record_response({"no": "usage"})

    assert usage.totals() == {
        "requests": 4,
        "input_tokens": 900 + 600 + 400 + 100,
        "cache_read_input_tokens": 100 + 400 + 100 + 200,
        "cache_creation_input_tokens": 50,
        "output_tokens": 200 + 300 + 50 + 30,
    }
    assert usage.summary() == "tokens: input 2,000 (cache read 800, cache write 50), output 580, 4 requests"
    usage.reset()
    assert usage.summary() is None


def test_claude_reply_usage_is_counted(monkeypatch) -> None:
    usage.reset()

    def handler(request: httpx.Request) -> httpx.Response:
        item = json.loads(json.loads(request.content)["messages"][0]["content"])["items"][0]
        text = json.dumps({"items": [{"id": item["id"], "translation": "阀门长 [[TRP_0000]]。"}]}, ensure_ascii=False)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": {"input_tokens": 1234, "output_tokens": 56}},
            request=request,
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    AnthropicProvider(AnthropicConfig(model="claude-haiku-5-5"), client=httpx.Client(transport=httpx.MockTransport(handler))).translate_batch([make_unit()])

    assert usage.totals() == {"requests": 1, "input_tokens": 1234, "output_tokens": 56}
    usage.reset()


def test_cli_prints_token_summary_even_on_failure(tmp_path, capsys) -> None:
    usage.reset()
    usage.record(input_tokens=10, output_tokens=2)
    code = main(["translate-markdown", str(tmp_path / "missing.md"), str(tmp_path / "out.md"), "--source-language", "en", "--target-language", "zh", "--provider", "deepseek", "--model", "deepseek-chat"])

    assert code == 1
    assert "tokens: input 10, output 2, 1 requests" in capsys.readouterr().err
    usage.reset()
