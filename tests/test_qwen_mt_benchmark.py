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


def test_unit_preserves_benchmark_literals_as_tokens():
    unit = unit_for(record())
    assert unit.protected_tokens == ["K12+340"]


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
