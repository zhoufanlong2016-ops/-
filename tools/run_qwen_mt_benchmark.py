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
        "required_terms": all(term["target"].casefold() in translation.casefold() for term in record["required_terms"]),
        "prompt_leakage": not any(marker in translation.casefold() for marker in (
            "unit_id", "source_text", "return json", "translate the following text into",
        )),
    }


def _atomic_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    os.replace(temporary, path)


def _batch_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """Keep one request batch within one language/glossary contract."""
    terms = tuple((entry["source"], entry["target"]) for entry in record["required_terms"])
    return record["source_language"], record["target_language"], terms


def run(records: list[dict[str, Any]], provider_factory, result_path: Path, *, model: str, max_attempts: int, retry_delay: float = 1.0) -> list[dict[str, Any]]:
    existing = {
        row["id"]: row for row in (
            json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()
        )
    } if result_path.exists() else {}
    pending = [record for record in records if not existing.get(record["id"], {}).get("success")]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in pending:
        groups.setdefault(_batch_key(record), []).append(record)

    for group in groups.values():
        provider = provider_factory(group[0])
        units = [unit_for(record) for record in group]
        batch_translate = getattr(provider, "translate_batch", None)
        started = time.perf_counter()
        attempts = 0
        translated = []
        error_code = ""
        for _ in range(max_attempts):
            attempts += 1
            try:
                if callable(batch_translate):
                    translated = list(batch_translate(units))
                    if len(translated) != len(units):
                        raise ValueError("batch translation count mismatch")
                    if {result.unit_id for result in translated} != {unit.id for unit in units}:
                        raise ValueError("batch translation IDs do not match source units")
                else:
                    translated = [provider.translate_unit(unit) for unit in units]
                error_code = ""
                break
            except Exception as error:
                translated = []
                error_code = getattr(error, "code", type(error).__name__)
                if attempts < max_attempts:
                    time.sleep(retry_delay * attempts)

        elapsed = round(time.perf_counter() - started, 3)
        by_id = {result.unit_id: result for result in translated}
        for record, unit in zip(group, units, strict=True):
            result = by_id.get(unit.id)
            translation = result.translation if result is not None else ""
            checks = checks_for(record, translation) if translation else {}
            success = bool(translation) and not error_code and all(checks.values())
            existing[record["id"]] = {
                "id": record["id"],
                "provider": "qwen_mt",
                "model": model,
                "attempts": attempts,
                "elapsed_seconds": elapsed,
                "success": success,
                "error_code": error_code or ("VALIDATION_FAILED" if not success else ""),
                "checks": checks,
                "translation": translation,
                "needs_manual_review": True,
                "reference_sent_to_model": False,
            }
            print(f"{record['id']}: {'ok' if success else 'failed'}", flush=True)
        _atomic_write(result_path, [existing[item["id"]] for item in records if item["id"] in existing])
    return [existing[record["id"]] for record in records]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/engineering_translation_100.jsonl"))
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--model", choices=("qwen-mt-plus", "qwen-mt-flash"), default="qwen-mt-flash")
    parser.add_argument("--glossary", type=Path, help="CSV/XLSX glossary to use for every benchmark unit")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if args.max_attempts < 1:
        raise ValueError("max-attempts must be at least 1")
    records = load_records(args.dataset)
    shared_glossary = Glossary.load(args.glossary) if args.glossary is not None else None
    with httpx.Client(trust_env=False) as client:
        def provider_factory(record: dict[str, Any]) -> QwenMTProvider:
            glossary = shared_glossary if shared_glossary is not None else glossary_for(record)
            return QwenMTProvider(QwenMTConfig(model=args.model), client=client, glossary=glossary)
        results = run(records, provider_factory, args.results, model=args.model, max_attempts=args.max_attempts)
    passed = sum(result["success"] for result in results)
    print(json.dumps({"records": len(results), "passed": passed, "failed": len(results) - passed}, ensure_ascii=False))
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
