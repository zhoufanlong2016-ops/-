from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

from run_qwen_mt_benchmark import checks_for, run, unit_for
from document_translator.core import TranslationResult, sha256_text


def record():
    return {
        "id": "eng-zh-001", "source_language": "zh-CN", "target_language": "en",
        "source_text": "阀门安装在K12+340。", "reference_translation": "Install valve at K12+340.",
        "required_terms": [{"source": "阀门", "target": "valve"}],
        "protected_literals": ["K12+340"],
    }


class Provider:
    def translate_unit(self, unit):
        text = "Install valve at K12+340."
        return TranslationResult(
            unit_id=unit.id, translation=text, provider="qwen_mt", model="qwen-mt-flash",
            prompt_version="qwen-mt-v1", glossary_version="test", source_hash=sha256_text(unit.source_text),
            result_hash=sha256_text(text), request_count=1, validation_status="valid",
        )


class BatchProvider(Provider):
    def __init__(self):
        self.batch_calls = 0

    def translate_batch(self, units):
        self.batch_calls += 1
        results = []
        for unit in units:
            text = f"Install valve at {unit.protected_tokens[0]}."
            results.append(TranslationResult(
                unit_id=unit.id, translation=text, provider="qwen_mt", model="qwen-mt-flash",
                prompt_version="qwen-mt-v1", glossary_version="test", source_hash=sha256_text(unit.source_text),
                result_hash=sha256_text(text), request_count=1, validation_status="valid",
            ))
        return results


def test_unit_preserves_benchmark_literals_as_tokens():
    unit = unit_for(record())
    assert unit.protected_tokens == ["K12+340"]
    assert "K12+340" in unit.source_text


def test_case_insensitive_english_term_check():
    assert checks_for(record(), "Install Valve at K12+340.")["required_terms"]


def test_checks_detect_required_term_and_prompt_leakage():
    assert all(checks_for(record(), "Install valve at K12+340.").values())
    assert not checks_for(record(), "unit_id")["prompt_leakage"]


def test_runner_never_emits_reference_and_resumes_success(tmp_path):
    source = record()
    results = tmp_path / "results.jsonl"
    first = run([source], lambda _: Provider(), results, model="qwen-mt-flash", max_attempts=1)
    second = run([source], lambda _: (_ for _ in ()).throw(AssertionError("should be cached")), results, model="qwen-mt-flash", max_attempts=1)
    assert first[0]["success"] and second[0]["success"]
    assert '"reference_translation"' not in results.read_text(encoding="utf-8")


def test_runner_batches_records_with_one_language_and_glossary_contract(tmp_path):
    first = record()
    second = {**record(), "id": "eng-zh-002", "source_text": "阀门安装在K12+350。", "protected_literals": ["K12+350"]}
    provider = BatchProvider()
    results = run(
        [first, second], lambda _: provider, tmp_path / "results.jsonl", model="qwen-mt-flash", max_attempts=1,
    )
    assert all(item["success"] for item in results)
    assert provider.batch_calls == 1
