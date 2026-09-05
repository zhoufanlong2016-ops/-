"""Run the engineering benchmark through Qwen-MT without exposing references to the model."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any

import httpx

from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id, sha256_text
from document_translator.providers import QwenMTConfig, QwenMTProvider
from document_translator.services import Glossary, GlossaryEntry


def load_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def unit_for(record: dict[str, Any]) -> TranslationUnit:
    data: dict[str, Any] = {
        "document_hash": sha256_text("benchmark\0" + record["id"] + "\0" + record["source_text"]),
        "format": DocumentFormat.MD,
        "location": DocumentLocation(part="benchmark", object_id=record["id"]),
        "source_language": record["source_language"],
        "target_language": record["target_language"],
        "source_text": record["source_text"],
        "protected_tokens": record["protected_literals"],
    }
    return TranslationUnit(id=generate_unit_id(**data), **data)


def glossary_for(record: dict[str, Any]) -> Glossary:
    return Glossary(
        entries=tuple(GlossaryEntry(**entry) for entry in record["required_terms"]),
        version="benchmark-" + record["id"],
    )


def checks_for(record: dict[str, Any], translation: str) -> dict[str, bool]:
    return {
        "protected_literals": all(
            translation.count(literal) == record["source_text"].count(literal)
            for literal in record["protected_literals"]
        ),
        "required_terms": all(term["target"] in translation for term in record["required_terms"]),
        "prompt_leakage": not any(marker in translation.casefold() for marker in (
            "unit_id", "source_text", "return json", "translate the following text into",
        )),
    }


def _atomic_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    os.replace(temporary, path)


def run(records: list[dict[str, Any]], provider_factory, result_path: Path, *, model: str, max_attempts: int) -> list[dict[str, Any]]:
    existing = {
        row["id"]: row for row in (
            json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()
        )
    } if result_path.exists() else {}
    for record in records:
        if existing.get(record["id"], {}).get("success"):
            continue
        started = time.perf_counter()
        translation = ""
        error_code = ""
        attempts = 0
        for _ in range(max_attempts):
            attempts += 1
            try:
                result = provider_factory(record).translate_unit(unit_for(record))
                translation = result.translation
                error_code = ""
                break
            except Exception as error:
                error_code = getattr(error, "code", type(error).__name__)
        checks = checks_for(record, translation) if translation else {}
        success = bool(translation) and not error_code and all(checks.values())
        existing[record["id"]] = {
            "id": record["id"],
            "provider": "qwen_mt",
            "model": model,
            "attempts": attempts,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "success": success,
            "error_code": error_code or ("VALIDATION_FAILED" if not success else ""),
            "checks": checks,
            "translation": translation,
            "needs_manual_review": True,
            "reference_sent_to_model": False,
        }
        _atomic_write(result_path, [existing[item["id"]] for item in records if item["id"] in existing])
        print(f"{record['id']}: {'ok' if success else 'failed'}", flush=True)
    return [existing[record["id"]] for record in records]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/engineering_translation_100.jsonl"))
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--model", choices=("qwen-mt-plus", "qwen-mt-flash"), default="qwen-mt-flash")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if args.max_attempts < 1:
        raise ValueError("max-attempts must be at least 1")
    records = load_records(args.dataset)
    with httpx.Client(trust_env=False) as client:
        def provider_factory(record: dict[str, Any]) -> QwenMTProvider:
            return QwenMTProvider(QwenMTConfig(model=args.model), client=client, glossary=glossary_for(record))
        results = run(records, provider_factory, args.results, model=args.model, max_attempts=args.max_attempts)
    passed = sum(result["success"] for result in results)
    print(json.dumps({"records": len(results), "passed": passed, "failed": len(results) - passed}, ensure_ascii=False))
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
