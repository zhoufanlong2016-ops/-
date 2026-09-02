"""Validate the offline engineering translation benchmark JSONL."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

LANGUAGE_PAIRS = {("zh-CN", "en"), ("en", "zh-CN")}
REQUIRED_FIELDS = {
    "id", "source_language", "target_language", "category", "source_text",
    "reference_translation", "required_terms", "protected_literals",
    "review_status", "notes",
}
ID_RE = re.compile(r"^eng-(zh|en)-([0-9]{3})$")
BAND_RE = re.compile(r"length_band=(short|medium|long)")


def _error(errors: list[str], line_no: int, message: str) -> None:
    errors.append(f"line {line_no}: {message}")


def validate_file(path: str | Path) -> dict[str, Any]:
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    source_lines: dict[str, int] = {}
    path = Path(path)

    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return {"errors": [f"cannot read {path}: {exc}"], "records": [], "stats": {}}

    for line_no, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            _error(errors, line_no, "blank line is not valid JSONL data")
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            _error(errors, line_no, f"invalid JSON: {exc.msg}")
            continue
        if not isinstance(item, dict):
            _error(errors, line_no, "record must be a JSON object")
            continue
        records.append(item)
        missing = REQUIRED_FIELDS - item.keys()
        if missing:
            _error(errors, line_no, f"missing fields: {sorted(missing)}")
        if set(item) < REQUIRED_FIELDS:
            continue
        record_id = item["id"]
        if not isinstance(record_id, str) or not ID_RE.fullmatch(record_id):
            _error(errors, line_no, "id must match eng-zh-NNN or eng-en-NNN")
        else:
            source_lines.setdefault(record_id, line_no)
            prefix, number = ID_RE.fullmatch(record_id).groups()
            expected_direction = ("zh-CN", "en") if prefix == "zh" else ("en", "zh-CN")
            if (item["source_language"], item["target_language"]) != expected_direction:
                _error(errors, line_no, "id direction does not match language direction")
            if int(number) < 1 or int(number) > 50:
                _error(errors, line_no, "directional id number must be 001..050")
        if (item["source_language"], item["target_language"]) not in LANGUAGE_PAIRS:
            _error(errors, line_no, "illegal language direction")
        for field in ("source_text", "reference_translation", "category", "notes"):
            if not isinstance(item[field], str) or not item[field].strip():
                _error(errors, line_no, f"{field} must be non-empty")
        if item["review_status"] not in {"candidate", "approved"}:
            _error(errors, line_no, "review_status must be candidate or approved")
        terms = item["required_terms"]
        if not isinstance(terms, list):
            _error(errors, line_no, "required_terms must be a list")
        else:
            for term in terms:
                if (not isinstance(term, dict) or set(term) != {"source", "target"}
                        or not isinstance(term["source"], str) or not term["source"]
                        or not isinstance(term["target"], str) or not term["target"]):
                    _error(errors, line_no, "each required_terms item must contain non-empty source and target")
        literals = item["protected_literals"]
        if not isinstance(literals, list):
            _error(errors, line_no, "protected_literals must be a list")
        else:
            for literal in literals:
                if not isinstance(literal, str) or not literal:
                    _error(errors, line_no, "protected_literals entries must be non-empty strings")
                elif literal not in item["source_text"]:
                    _error(errors, line_no, f"protected literal not found in source_text: {literal!r}")
        band = BAND_RE.search(item["notes"])
        if not band:
            _error(errors, line_no, "notes must declare length_band=short|medium|long")

    ids = [item.get("id") for item in records]
    duplicates = [record_id for record_id, count in Counter(ids).items() if count > 1]
    for record_id in duplicates:
        _error(errors, source_lines.get(record_id, 0), f"duplicate id: {record_id}")
    if len(records) != 100:
        errors.append(f"record count is {len(records)}, expected exactly 100")

    direction_counts = Counter(
        f"{item.get('source_language')}->{item.get('target_language')}" for item in records
    )
    category_counts = Counter(item.get("category") for item in records)
    length_counts = Counter()
    for item in records:
        match = BAND_RE.search(str(item.get("notes", "")))
        if match:
            length_counts[match.group(1)] += 1
    expected_ids = {f"eng-zh-{i:03d}" for i in range(1, 51)} | {f"eng-en-{i:03d}" for i in range(1, 51)}
    if set(ids) != expected_ids:
        errors.append("IDs are not the complete stable eng-zh-001..050 and eng-en-001..050 set")
    return {
        "errors": errors,
        "records": records,
        "stats": {
            "total": len(records),
            "directions": dict(sorted(direction_counts.items())),
            "categories": dict(sorted(category_counts.items(), key=lambda pair: str(pair[0]))),
            "length_bands": dict(sorted(length_counts.items())),
            "duplicate_ids": duplicates,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="benchmarks/engineering_translation_100.jsonl")
    args = parser.parse_args(argv)
    result = validate_file(args.path)
    print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
    if result["errors"]:
        print("VALIDATION_FAILED", file=sys.stderr)
        for error in result["errors"]:
            print(error, file=sys.stderr)
        return 1
    print("VALIDATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
