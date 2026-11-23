from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

MAX_TERM_CHARS = 12
MAX_CAT_CHARS = 8
HINT_MIN_CHARS = 15
HINT_MAX_CHARS = 40

DEFAULT_HELD_OUT_TOPICS: tuple[tuple[str, str], ...] = (
    ("電子レンジの掃除", "賃貸1LDK、手間をかけずに週1で済ませたい"),
    ("月曜朝の出社準備", "朝の時間に余裕がなくて毎日バタつく"),
    ("深夜のコンビニ散歩", "気分転換を兼ねて新しいアイデアを拾いたい"),
    ("確定申告の領収書整理", "個人開発の副業経費をストレスなく管理したい"),
    ("開発環境のバックアップ", "PC買い替え時でも1時間で復旧したい"),
    ("オンライン勉強会の進行", "初参加の人でも発言しやすい場にしたい"),
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_yaml_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config at {path} must be a YAML mapping.")
    model_id = raw.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError(f"Config at {path} requires a non-empty string 'model_id'.")
    if not isinstance(raw.get("lora"), dict):
        raise ValueError(f"Config at {path} requires a mapping 'lora'.")
    if not isinstance(raw.get("train"), dict):
        raise ValueError(f"Config at {path} requires a mapping 'train'.")
    return raw


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        stripped = raw_line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError(f"{path}:{line_no} must be a JSON object.")
        rows.append(parsed)
    return rows


def strip_code_fences(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = [
        line for line in stripped.splitlines() if not line.strip().startswith("```")
    ]
    return "\n".join(lines).strip()


def extract_json_object(raw: str) -> dict[str, Any] | None:
    cleaned = strip_code_fences(raw)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_bad_japanese_text(value: object, *, max_len: int) -> bool:
    if not isinstance(value, str):
        return True
    if not value or not value.strip() or len(value) > max_len:
        return True
    return any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in value)


@dataclass(frozen=True, slots=True)
class TreeQualityReport:
    json_valid: bool
    root_matches: bool
    strict_valid: bool
    branch_count: int
    l2_count: int
    l3_count: int
    total_terms: int
    unique_terms: int
    duplicate_term_count: int
    distinct_cats: int
    cat_violation_count: int
    valid_hint_count: int
    first_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_raw_association_json(
    raw: str,
    *,
    expected_root: str,
) -> TreeQualityReport:
    obj = extract_json_object(raw)
    if obj is None:
        return TreeQualityReport(
            json_valid=False,
            root_matches=False,
            strict_valid=False,
            branch_count=0,
            l2_count=0,
            l3_count=0,
            total_terms=0,
            unique_terms=0,
            duplicate_term_count=0,
            distinct_cats=0,
            cat_violation_count=0,
            valid_hint_count=0,
            first_error="invalid or missing JSON object",
        )

    errors: list[str] = []
    if set(obj) != {"root", "branches"}:
        errors.append(f"top-level keys {sorted(obj)} != ['branches', 'root']")

    root_val = obj.get("root")
    root_matches = isinstance(root_val, str) and root_val == expected_root
    if not root_matches:
        errors.append(f"root {root_val!r} != {expected_root!r}")

    branches = obj.get("branches")
    if not isinstance(branches, list):
        errors.append("branches is not a list")
        return TreeQualityReport(
            json_valid=True,
            root_matches=root_matches,
            strict_valid=False,
            branch_count=0,
            l2_count=0,
            l3_count=0,
            total_terms=0,
            unique_terms=0,
            duplicate_term_count=0,
            distinct_cats=0,
            cat_violation_count=0,
            valid_hint_count=0,
            first_error=errors[0],
        )

    branch_count = len(branches)
    if branch_count != 2:
        errors.append(f"branches count {branch_count} != 2")

    seen_terms: set[str] = {expected_root}
    all_terms: list[str] = []
    all_cats: set[str] = set()
    branch_cats: list[str] = []
    duplicate_term_count = 0
    cat_violation_count = 0
    l2_count = 0
    l3_count = 0
    valid_hint_count = 0

    def record_term(value: object, label: str) -> None:
        nonlocal duplicate_term_count
        if _is_bad_japanese_text(value, max_len=MAX_TERM_CHARS):
            errors.append(f"bad {label} term {value!r}")
        if isinstance(value, str) and value:
            all_terms.append(value)
            if value in seen_terms:
                duplicate_term_count += 1
                errors.append(f"duplicate term {value!r}")
            else:
                seen_terms.add(value)

    for branch in branches:
        if not isinstance(branch, dict):
            errors.append("branch item is not a dict")
            continue
        if set(branch) != {"l1", "cat", "l2"}:
            errors.append(f"branch keys {sorted(branch)} != ['cat', 'l1', 'l2']")

        l1 = branch.get("l1")
        bcat = branch.get("cat")
        l2_list = branch.get("l2")
        record_term(l1, "l1")

        if _is_bad_japanese_text(bcat, max_len=MAX_CAT_CHARS):
            errors.append(f"bad l1 cat {bcat!r}")
        if isinstance(bcat, str) and bcat:
            all_cats.add(bcat)
            branch_cats.append(bcat)

        if not isinstance(l2_list, list):
            errors.append("l2 is not a list")
            continue
        l2_count += len(l2_list)
        if len(l2_list) != 2:
            errors.append(f"l2 count {len(l2_list)} != 2")

        l2_cats: list[str] = []
        for l2 in l2_list:
            if not isinstance(l2, dict):
                errors.append("l2 item is not a dict")
                continue
            if set(l2) != {"term", "cat", "l3"}:
                errors.append(f"l2 keys {sorted(l2)} != ['cat', 'l3', 'term']")

            l2_term = l2.get("term")
            l2_cat = l2.get("cat")
            l3_list = l2.get("l3")
            record_term(l2_term, "l2")

            if _is_bad_japanese_text(l2_cat, max_len=MAX_CAT_CHARS):
                errors.append(f"bad l2 cat {l2_cat!r}")
            if isinstance(l2_cat, str) and l2_cat:
                all_cats.add(l2_cat)
                l2_cats.append(l2_cat)
                if isinstance(bcat, str) and l2_cat == bcat:
                    cat_violation_count += 1
                    errors.append(f"l2 cat {l2_cat!r} repeats parent cat")

            if not isinstance(l3_list, list):
                errors.append("l3 is not a list")
                continue
            l3_count += len(l3_list)
            if len(l3_list) != 2:
                errors.append(f"l3 count {len(l3_list)} != 2")

            l3_cats: list[str] = []
            for l3 in l3_list:
                if not isinstance(l3, dict):
                    errors.append("l3 item is not a dict")
                    continue
                if set(l3) != {"term", "cat", "hint"}:
                    errors.append(f"l3 keys {sorted(l3)} != ['cat', 'hint', 'term']")

                l3_term = l3.get("term")
                l3_cat = l3.get("cat")
                hint = l3.get("hint")
                record_term(l3_term, "l3")

                if _is_bad_japanese_text(l3_cat, max_len=MAX_CAT_CHARS):
                    errors.append(f"bad l3 cat {l3_cat!r}")
                if isinstance(l3_cat, str) and l3_cat:
                    all_cats.add(l3_cat)
                    l3_cats.append(l3_cat)
                    if isinstance(l2_cat, str) and l3_cat == l2_cat:
                        cat_violation_count += 1
                        errors.append(f"l3 cat {l3_cat!r} repeats parent cat")

                if (
                    isinstance(hint, str)
                    and HINT_MIN_CHARS <= len(hint) <= HINT_MAX_CHARS
                ):
                    valid_hint_count += 1
                else:
                    errors.append(f"bad hint {hint!r}")

            if len(l3_cats) != len(set(l3_cats)):
                cat_violation_count += len(l3_cats) - len(set(l3_cats))
                errors.append("sibling l3 cats duplicate")

        if len(l2_cats) != len(set(l2_cats)):
            cat_violation_count += len(l2_cats) - len(set(l2_cats))
            errors.append("sibling l2 cats duplicate")

    if len(branch_cats) != len(set(branch_cats)):
        cat_violation_count += len(branch_cats) - len(set(branch_cats))
        errors.append("sibling l1 cats duplicate")

    return TreeQualityReport(
        json_valid=True,
        root_matches=root_matches,
        strict_valid=not errors,
        branch_count=branch_count,
        l2_count=l2_count,
        l3_count=l3_count,
        total_terms=len(all_terms),
        unique_terms=len(set(all_terms)),
        duplicate_term_count=duplicate_term_count,
        distinct_cats=len(all_cats),
        cat_violation_count=cat_violation_count,
        valid_hint_count=valid_hint_count,
        first_error=errors[0] if errors else None,
    )
