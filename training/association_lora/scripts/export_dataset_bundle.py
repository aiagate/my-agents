from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import read_jsonl, sha256_file, sha256_text
from my_agents.domain.association_tree import ASSOCIATION_SYSTEM_PROMPT

DEFAULT_SRC_DIR = Path.home() / ".cache" / "my-agents-comp" / "mlx_lora" / "data"


def _validate_rows(
    rows: list[dict[str, Any]],
    *,
    filename: str,
) -> set[str]:
    system_hashes: set[str] = set()
    for idx, row in enumerate(rows, start=1):
        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) < 3:
            raise ValueError(
                f"{filename} row {idx} must contain at least 3 chat messages."
            )
        roles = [msg.get("role") if isinstance(msg, dict) else None for msg in messages]
        if roles[0] != "system" or roles[-2] != "user" or roles[-1] != "assistant":
            raise ValueError(f"{filename} row {idx} has unexpected roles: {roles!r}.")
        for msg in messages:
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), str):
                raise ValueError(
                    f"{filename} row {idx} contains message without string content."
                )
        system_hashes.add(sha256_text(messages[0]["content"]))
    return system_hashes


def export_bundle(
    src_dir: Path,
    out_dir: Path,
    *,
    allow_prompt_mismatch: bool = False,
) -> dict[str, Any]:
    train_src = src_dir / "train.jsonl"
    valid_src = src_dir / "valid.jsonl"
    if not train_src.is_file():
        raise FileNotFoundError(f"Missing required train.jsonl at {train_src}")
    if not valid_src.is_file():
        raise FileNotFoundError(f"Missing required valid.jsonl at {valid_src}")

    train_rows = read_jsonl(train_src)
    valid_rows = read_jsonl(valid_src)
    seen_prompt_hashes = _validate_rows(train_rows, filename="train.jsonl")
    seen_prompt_hashes.update(_validate_rows(valid_rows, filename="valid.jsonl"))

    test_src = src_dir / "test.jsonl"
    test_rows: list[dict[str, Any]] | None = None
    if test_src.is_file():
        test_rows = read_jsonl(test_src)
        seen_prompt_hashes.update(_validate_rows(test_rows, filename="test.jsonl"))

    domain_prompt_sha256 = sha256_text(ASSOCIATION_SYSTEM_PROMPT)
    prompt_matches_domain = seen_prompt_hashes == {domain_prompt_sha256}
    if not prompt_matches_domain and not allow_prompt_mismatch:
        raise ValueError(
            "Dataset system prompt does not match "
            "my_agents.domain.association_tree.ASSOCIATION_SYSTEM_PROMPT "
            f"(domain={domain_prompt_sha256[:12]}, "
            f"dataset={sorted(h[:12] for h in seen_prompt_hashes)}). "
            "Pass --allow-prompt-mismatch to override."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    train_dst = out_dir / "train.jsonl"
    valid_dst = out_dir / "valid.jsonl"
    shutil.copy2(train_src, train_dst)
    shutil.copy2(valid_src, valid_dst)

    test_sha256: str | None = None
    if test_rows is not None:
        test_dst = out_dir / "test.jsonl"
        shutil.copy2(test_src, test_dst)
        test_sha256 = sha256_file(test_dst)

    source_metadata: dict[str, Any] | None = None
    metadata_src = src_dir / "metadata.json"
    if metadata_src.is_file():
        parsed_meta = json.loads(metadata_src.read_text(encoding="utf-8"))
        if isinstance(parsed_meta, dict):
            source_metadata = parsed_meta

    manifest: dict[str, Any] = {
        "exported_at_utc": datetime.now(UTC).isoformat(),
        "source_dir": str(src_dir),
        "prompt_sha256": domain_prompt_sha256,
        "dataset_system_prompt_sha256s": sorted(seen_prompt_hashes),
        "prompt_matches_domain": prompt_matches_domain,
        "train_rows": len(train_rows),
        "valid_rows": len(valid_rows),
        "test_rows": len(test_rows) if test_rows is not None else None,
        "train_sha256": sha256_file(train_dst),
        "valid_sha256": sha256_file(valid_dst),
        "test_sha256": test_sha256,
        "source_metadata": source_metadata,
    }
    (out_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and export an association LoRA JSONL dataset bundle "
            "for upload to Google Drive / Colab."
        )
    )
    parser.add_argument(
        "--src-dir",
        type=Path,
        default=DEFAULT_SRC_DIR,
        help=f"Source dataset directory (default: {DEFAULT_SRC_DIR})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Destination bundle directory",
    )
    parser.add_argument(
        "--allow-prompt-mismatch",
        action="store_true",
        help="Export even when dataset system prompt differs from ASSOCIATION_SYSTEM_PROMPT",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = export_bundle(
        args.src_dir.expanduser(),
        args.out_dir.expanduser(),
        allow_prompt_mismatch=args.allow_prompt_mismatch,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
