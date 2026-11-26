from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Iterator, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

from common import DEFAULT_HELD_OUT_TOPICS, evaluate_raw_association_json, read_jsonl
from flow_res import Err, Ok, Result, is_err
from my_agents.contracts.ports.association_generator import (
    AssociationGeneratorError,
    AssociationGeneratorErrorType,
)
from my_agents.domain.association_tree import (
    ASSOCIATION_SYSTEM_PROMPT,
    build_association_user_prompt,
    parse_association_tree,
)
from my_agents.infrastructure.error_formatting import format_exception
from my_agents.infrastructure.mlx_lora_association_generator import (
    GENERATION_MAX_TOKENS,
    GENERATION_REPETITION_CONTEXT_SIZE,
    GENERATION_REPETITION_PENALTY,
    GENERATION_TEMPERATURE,
    MlxLoraAssociationGenerator,
)


def _extract_topic_and_context(user_content: str) -> tuple[str, str]:
    topic = ""
    context = ""
    for line in user_content.splitlines():
        if line.startswith("トピック:"):
            topic = line.removeprefix("トピック:").strip()
        elif line.startswith("背景:"):
            context = line.removeprefix("背景:").strip()
    if not topic:
        raise ValueError(f"Could not extract topic from user prompt: {user_content!r}")
    return topic, context


def _load_topics(
    topics_jsonl: Path | None,
    limit: int | None,
) -> list[tuple[str, str]]:
    if topics_jsonl is None:
        topics = list(DEFAULT_HELD_OUT_TOPICS)
    else:
        rows = read_jsonl(topics_jsonl)
        topics = []
        for row in rows:
            messages = row.get("messages")
            if not isinstance(messages, list):
                continue
            user_msg = next(
                (
                    msg.get("content")
                    for msg in messages
                    if isinstance(msg, dict) and msg.get("role") == "user"
                ),
                None,
            )
            if isinstance(user_msg, str):
                topics.append(_extract_topic_and_context(user_msg))
    if limit is not None and limit > 0:
        topics = topics[:limit]
    return topics


class _TrustRemoteCodeMlxGenerator:
    def __init__(self, model_name: str, adapter_path: Path | None) -> None:
        self._model_name = model_name
        self._adapter_path = adapter_path
        self._loaded: tuple[Any, Any] | None = None
        self._load_lock = asyncio.Lock()
        self._generate_lock = asyncio.Lock()

    def _load_sync(self) -> tuple[Any, Any]:
        mlx_lm = import_module("mlx_lm")
        return mlx_lm.load(
            self._model_name,
            adapter_path=str(self._adapter_path)
            if self._adapter_path is not None
            else None,
            tokenizer_config={"trust_remote_code": True},
            trust_remote_code=True,
        )

    async def _ensure_loaded(self) -> tuple[Any, Any]:
        if self._loaded is not None:
            return self._loaded
        async with self._load_lock:
            if self._loaded is None:
                self._loaded = await asyncio.to_thread(self._load_sync)
            return self._loaded

    def _generate_sync(
        self,
        model: Any,
        tokenizer: Any,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        mlx_lm = import_module("mlx_lm")
        sample_utils = import_module("mlx_lm.sample_utils")
        prompt = tokenizer.apply_chat_template(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
        chunks: Iterator[str] = (
            chunk.text
            for chunk in mlx_lm.stream_generate(
                model,
                tokenizer,
                prompt,
                max_tokens=GENERATION_MAX_TOKENS,
                sampler=sample_utils.make_sampler(temp=GENERATION_TEMPERATURE),
                logits_processors=sample_utils.make_logits_processors(
                    repetition_penalty=GENERATION_REPETITION_PENALTY,
                    repetition_context_size=GENERATION_REPETITION_CONTEXT_SIZE,
                ),
            )
        )
        return "".join(chunks)

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Result[str, AssociationGeneratorError]:
        try:
            model, tokenizer = await self._ensure_loaded()
        except Exception as exc:
            return Err(
                AssociationGeneratorError(
                    type=AssociationGeneratorErrorType.MODEL_UNAVAILABLE,
                    message=format_exception(exc),
                )
            )
        try:
            async with self._generate_lock:
                text = await asyncio.to_thread(
                    self._generate_sync,
                    model,
                    tokenizer,
                    system_prompt,
                    user_prompt,
                )
        except Exception as exc:
            return Err(
                AssociationGeneratorError(
                    type=AssociationGeneratorErrorType.GENERATION_FAILED,
                    message=format_exception(exc),
                )
            )
        return Ok(text)


async def evaluate_model(
    *,
    model: str,
    adapter_path: Path | None,
    topics: Sequence[tuple[str, str]],
    trust_remote_code: bool = False,
) -> dict[str, Any]:
    generator: MlxLoraAssociationGenerator | _TrustRemoteCodeMlxGenerator
    if trust_remote_code:
        generator = _TrustRemoteCodeMlxGenerator(model, adapter_path)
    else:
        generator = MlxLoraAssociationGenerator.from_settings(model, adapter_path)
    records: list[dict[str, Any]] = []

    for topic, context in topics:
        started = time.perf_counter()
        gen_result = await generator.generate(
            ASSOCIATION_SYSTEM_PROMPT,
            build_association_user_prompt(topic, context),
        )
        elapsed_s = round(time.perf_counter() - started, 3)

        if is_err(gen_result):
            records.append(
                {
                    "topic": topic,
                    "context": context,
                    "elapsed_s": elapsed_s,
                    "generation_error": (
                        f"{gen_result.error.type.name}: {gen_result.error.message}"
                    ),
                    "prod_parse_ok": False,
                    "prod_ideas_count": 0,
                    "quality": None,
                    "raw": None,
                }
            )
            continue

        raw_text = gen_result.value
        parsed = parse_association_tree(raw_text, root=topic)
        prod_parse_ok = not is_err(parsed)
        prod_ideas_count = 0 if is_err(parsed) else len(parsed.value.lateral_ideas)
        quality = evaluate_raw_association_json(raw_text, expected_root=topic)
        records.append(
            {
                "topic": topic,
                "context": context,
                "elapsed_s": elapsed_s,
                "generation_error": None,
                "prod_parse_ok": prod_parse_ok,
                "prod_ideas_count": prod_ideas_count,
                "quality": quality.to_dict(),
                "raw": raw_text,
            }
        )

    total = len(records)
    prod_ok = sum(1 for rec in records if rec["prod_parse_ok"])
    strict_ok = sum(
        1 for rec in records if rec["quality"] and rec["quality"]["strict_valid"]
    )
    qualities = [rec["quality"] for rec in records if rec["quality"] is not None]
    n_qual = max(len(qualities), 1)

    summary = {
        "total_topics": total,
        "prod_parse_ok": prod_ok,
        "prod_parse_rate": round(prod_ok / max(total, 1), 3),
        "strict_valid": strict_ok,
        "strict_valid_rate": round(strict_ok / max(total, 1), 3),
        "avg_l3_leaves": round(sum(q["l3_count"] for q in qualities) / n_qual, 2),
        "avg_unique_terms": round(
            sum(q["unique_terms"] for q in qualities) / n_qual, 2
        ),
        "avg_total_terms": round(sum(q["total_terms"] for q in qualities) / n_qual, 2),
        "total_duplicate_terms": sum(q["duplicate_term_count"] for q in qualities),
        "avg_distinct_cats": round(
            sum(q["distinct_cats"] for q in qualities) / n_qual, 2
        ),
        "total_cat_violations": sum(q["cat_violation_count"] for q in qualities),
        "avg_valid_hints": round(
            sum(q["valid_hint_count"] for q in qualities) / n_qual, 2
        ),
        "cold_first_call_s": records[0]["elapsed_s"] if records else 0.0,
        "avg_elapsed_s": round(
            sum(rec["elapsed_s"] for rec in records) / max(total, 1), 3
        ),
    }

    return {
        "model": model,
        "adapter_path": str(adapter_path) if adapter_path is not None else None,
        "summary": summary,
        "topics": records,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"Model: {report['model']} | Adapter: {report['adapter_path'] or '(none)'}")
    for rec in report["topics"]:
        q = rec["quality"]
        if rec["generation_error"] is not None:
            print(f"- {rec['topic']}: GEN_ERROR ({rec['generation_error']})")
            continue
        print(
            f"- {rec['topic']}: prod_ok={rec['prod_parse_ok']} "
            f"strict={q['strict_valid']} l3={q['l3_count']} "
            f"terms={q['unique_terms']}/{q['total_terms']} dup={q['duplicate_term_count']} "
            f"cats={q['distinct_cats']} cat_err={q['cat_violation_count']} "
            f"hints={q['valid_hint_count']} ({rec['elapsed_s']}s)"
        )
        if q["first_error"]:
            print(f"    first_error: {q['first_error']}")

    s = report["summary"]
    print("\n=== Summary ===")
    print(
        f"prod_parse={s['prod_parse_ok']}/{s['total_topics']} ({s['prod_parse_rate']}) | "
        f"strict_valid={s['strict_valid']}/{s['total_topics']} ({s['strict_valid_rate']})"
    )
    print(
        f"avg_l3={s['avg_l3_leaves']} avg_terms={s['avg_unique_terms']}/{s['avg_total_terms']} "
        f"dup_terms={s['total_duplicate_terms']} avg_cats={s['avg_distinct_cats']} "
        f"cat_violations={s['total_cat_violations']} avg_hints={s['avg_valid_hints']}"
    )
    print(
        f"cold_first_call={s['cold_first_call_s']}s avg_latency={s['avg_elapsed_s']}s"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate a local MLX association model with the production "
            "MlxLoraAssociationGenerator and parse_association_tree."
        )
    )
    parser.add_argument("--model", required=True, help="HF model ID or local MLX dir")
    parser.add_argument("--adapter-path", type=Path, default=None)
    parser.add_argument("--topics-jsonl", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    topics = _load_topics(args.topics_jsonl, args.limit)
    adapter_path = (
        args.adapter_path.expanduser() if args.adapter_path is not None else None
    )
    report = asyncio.run(
        evaluate_model(
            model=args.model,
            adapter_path=adapter_path,
            topics=topics,
            trust_remote_code=args.trust_remote_code,
        )
    )
    _print_report(report)
    if args.out is not None:
        out_path = args.out.expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Saved report to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
