"""Run the engineering benchmark through the configured OpenAI Responses provider."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any

import httpx

from document_translator.providers import OpenAIConfig, OpenAIProvider
from run_qwen_mt_benchmark import (
    _batch_key,
    _atomic_write,
    checks_for,
    glossary_for,
    load_records,
    unit_for,
)
from document_translator.services import Glossary


def run(records: list[dict[str, Any]], provider_factory, result_path: Path, *, model: str, max_attempts: int) -> list[dict[str, Any]]:
    existing = {
        row["id"]: row
        for row in (json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines())
    } if result_path.exists() else {}
    pending = [record for record in records if not existing.get(record["id"], {}).get("success")]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in pending:
        groups.setdefault(_batch_key(record), []).append(record)

    for group in groups.values():
        provider = provider_factory(group[0])
        units = [unit_for(record) for record in group]
        started = time.perf_counter()
        attempts = 0
        translated = []
        error_code = ""
        for _ in range(max_attempts):
            attempts += 1
            try:
                translated = list(provider.translate_batch(units))
                if len(translated) != len(units) or {item.unit_id for item in translated} != {unit.id for unit in units}:
                    raise ValueError("batch translation IDs do not match source units")
                error_code = ""
                break
            except Exception as error:
                translated = []
                error_code = getattr(error, "code", type(error).__name__)
                if attempts < max_attempts:
                    time.sleep(attempts)

        elapsed = round(time.perf_counter() - started, 3)
        by_id = {item.unit_id: item for item in translated}
        for record, unit in zip(group, units, strict=True):
            result = by_id.get(unit.id)
            translation = result.translation if result is not None else ""
            checks = checks_for(record, translation) if translation else {}
            success = bool(translation) and not error_code and all(checks.values())
            existing[record["id"]] = {
                "id": record["id"], "provider": "openai", "model": model,
                "attempts": attempts, "elapsed_seconds": elapsed, "success": success,
                "error_code": error_code or ("VALIDATION_FAILED" if not success else ""),
                "checks": checks, "translation": translation,
                "needs_manual_review": True, "reference_sent_to_model": False,
            }
            print(f"{record['id']}: {'ok' if success else 'failed'}", flush=True)
        _atomic_write(result_path, [existing[item["id"]] for item in records if item["id"] in existing])
    return [existing[record["id"]] for record in records]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/engineering_translation_100.jsonl"))
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--model", choices=("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.4", "gpt-4o", "gpt-4o-mini"), default="gpt-5.6-terra")
    parser.add_argument("--glossary", type=Path, help="CSV/XLSX glossary to use for every benchmark unit")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if args.max_attempts < 1:
        raise ValueError("max-attempts must be at least 1")
    records = load_records(args.dataset)
    shared_glossary = Glossary.load(args.glossary) if args.glossary is not None else None
    with httpx.Client(trust_env=False) as client:
        def provider_factory(record: dict[str, Any]) -> OpenAIProvider:
            glossary: Glossary = shared_glossary if shared_glossary is not None else glossary_for(record)
            return OpenAIProvider(OpenAIConfig(model=args.model), client=client, glossary=glossary)
        results = run(records, provider_factory, args.results, model=args.model, max_attempts=args.max_attempts)
    passed = sum(result["success"] for result in results)
    print(json.dumps({"records": len(results), "passed": passed, "failed": len(results) - passed}, ensure_ascii=False))
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
