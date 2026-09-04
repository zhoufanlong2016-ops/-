"""Command-line entry point for the stage-one Markdown translator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

import httpx

from .providers import LocalLlamaConfig, LocalLlamaProvider, QwenMTConfig, QwenMTProvider
from .services import MarkdownTranslationService, TranslationCache


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="document-translator",
        description="Translate one Markdown file using a configured translation provider.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    translate = subparsers.add_parser("translate-markdown", help="translate a Markdown source file")
    translate.add_argument("source", type=Path, metavar="SOURCE")
    translate.add_argument("destination", type=Path, metavar="DESTINATION")
    translate.add_argument("--source-language", required=True)
    translate.add_argument("--target-language", required=True)
    translate.add_argument(
        "--provider",
        choices=("local-llama", "qwen-mt"),
        default="qwen-mt",
        help="translation provider (default: qwen-mt)",
    )
    translate.add_argument(
        "--model",
        help="provider model (default: qwen-mt-plus for qwen-mt; local-model for local-llama)",
    )
    translate.add_argument("--local-endpoint", help="local llama-server endpoint (local-llama only)")
    translate.add_argument("--cache", type=Path, metavar="PATH", help="explicit SQLite cache path")
    translate.add_argument("--translation-mode", default="default")
    translate.add_argument("--overwrite", action="store_true", help="allow replacement of DESTINATION")
    return parser


def _provider_for(args: argparse.Namespace, client: httpx.Client):
    if args.provider == "qwen-mt":
        if args.local_endpoint is not None:
            raise ValueError("--local-endpoint is only valid with --provider local-llama")
        config = QwenMTConfig(model=args.model if args.model is not None else "qwen-mt-plus")
        return QwenMTProvider(config, client=client)

    config = LocalLlamaConfig(
        model=args.model if args.model is not None else "local-model",
        endpoint=args.local_endpoint if args.local_endpoint is not None else LocalLlamaConfig().endpoint,
    )
    return LocalLlamaProvider(config, client=client)


def _translate_markdown(args: argparse.Namespace) -> int:
    if args.source.resolve() == args.destination.resolve():
        raise ValueError("source and destination paths must differ")

    cache: TranslationCache | None = None
    try:
        with httpx.Client() as client:
            provider = _provider_for(args, client)
            if args.cache is not None:
                cache = TranslationCache(args.cache)
            service = MarkdownTranslationService(
                provider,
                cache,
                translation_mode=args.translation_mode,
            )
            outcome = service.translate_file(
                args.source,
                args.destination,
                source_language=args.source_language,
                target_language=args.target_language,
                overwrite=args.overwrite,
            )
    finally:
        if cache is not None:
            cache.close()

    print(
        f"translated {len(outcome.units)} units; cache hits={outcome.cache_hits}; "
        f"cache misses={outcome.cache_misses}; output={args.destination}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _translate_markdown(args)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
