import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from validate_benchmark import validate_file


BENCHMARK = Path(__file__).parents[1] / "benchmarks" / "engineering_translation_100.jsonl"


def load_lines(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "sample.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_normal_100_records() -> None:
    result = validate_file(BENCHMARK)
    assert result["errors"] == []
    assert result["stats"]["total"] == 100
    assert result["stats"]["directions"] == {"en->zh-CN": 50, "zh-CN->en": 50}


def test_count_error(tmp_path: Path) -> None:
    line = BENCHMARK.read_text(encoding="utf-8").splitlines()[0]
    result = validate_file(load_lines(tmp_path, [line]))
    assert any("expected exactly 100" in error for error in result["errors"])


def test_duplicate_id(tmp_path: Path) -> None:
    lines = BENCHMARK.read_text(encoding="utf-8").splitlines()
    result = validate_file(load_lines(tmp_path, [lines[0], lines[0]]))
    assert any("duplicate id" in error for error in result["errors"])


def test_direction_count_error(tmp_path: Path) -> None:
    lines = BENCHMARK.read_text(encoding="utf-8").splitlines()[:2]
    result = validate_file(load_lines(tmp_path, lines))
    assert result["stats"]["directions"] == {"zh-CN->en": 2}


def test_empty_reference_translation(tmp_path: Path) -> None:
    item = json.loads(BENCHMARK.read_text(encoding="utf-8").splitlines()[0])
    item["reference_translation"] = ""
    result = validate_file(load_lines(tmp_path, [json.dumps(item, ensure_ascii=False)]))
    assert any("reference_translation must be non-empty" in error for error in result["errors"])


def test_missing_protected_literal(tmp_path: Path) -> None:
    item = json.loads(BENCHMARK.read_text(encoding="utf-8").splitlines()[1])
    item["protected_literals"] = ["NOT-IN-SOURCE"]
    result = validate_file(load_lines(tmp_path, [json.dumps(item, ensure_ascii=False)]))
    assert any("protected literal not found" in error for error in result["errors"])


def test_invalid_json(tmp_path: Path) -> None:
    result = validate_file(load_lines(tmp_path, ["{not-json"]))
    assert any("invalid JSON" in error for error in result["errors"])


def test_invalid_review_status(tmp_path: Path) -> None:
    item = json.loads(BENCHMARK.read_text(encoding="utf-8").splitlines()[0])
    item["review_status"] = "draft"
    result = validate_file(load_lines(tmp_path, [json.dumps(item, ensure_ascii=False)]))
    assert any("review_status must be candidate or approved" in error for error in result["errors"])
