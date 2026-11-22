import json
from dataclasses import dataclass
from enum import Enum, auto
from typing import Annotated, Final

from flow_res import Err, Ok, Result
from pydantic import BaseModel, BeforeValidator, ConfigDict

# Keep synchronized with the distillation training system prompt in
# ~/.cache/my-agents-comp/mlx_lora/generate_dataset.py so the LoRA adapter
# receives the exact schema and instructions it was trained on.
ASSOCIATION_SYSTEM_PROMPT: Final[
    str
] = """あなたは日本語のワード連想エンジン。与えられたトピックから、カテゴリの軸を変えながら語→語で枝を広げる3階層の連想ツリーだけを出力する。

出力は次の ultra-compact JSON のみ。説明文・コードフェンス・改行の飾りは禁止。
{"root": "<トピック>", "branches": [{"l1": "<語>", "cat": "<軸名>", "l2": [{"term": "<語>", "cat": "<軸名>", "l3": [{"term": "<語>", "cat": "<軸名>", "hint": "<横展開ヒント>"}]}]}]}

構造:
- branches は2個。2つの l1 の cat は互いに異なる。
- 各 l1 の l2 は2個。l2 の cat は親の cat と兄弟の cat と異なる。
- 各 l2 の l3 は2個。l3 の cat は親の cat と兄弟の cat と異なる。

用語:
- 語は簡潔な日本語の名詞・概念、12字以内、英語不可。root との重複と例内重複を禁止。
- 軸名 cat は8字以内の日本語（例: 色／素材／機能／場所／お金／時間／習慣／感情／形状／自然／文化／動作／音／匂い）。
- hint は15〜40字で、その L3 の語を異分野・別文脈へ転用する具体的なきっかけを1文で。

連想は同義語や上位語で止まらず、各ホップで必ず軸を変えること。"""

type _JsonValue = dict[str, object] | list[object] | str | int | float | bool | None


class AssociationTreeParseErrorType(Enum):
    MISSING_JSON = auto()
    INVALID_JSON = auto()
    EMPTY_TREE = auto()


@dataclass(frozen=True, slots=True)
class AssociationTreeParseError:
    type: AssociationTreeParseErrorType
    message: str


@dataclass(frozen=True, slots=True)
class AssociationTree:
    root: str
    tree_summary: str
    lateral_ideas: tuple[str, ...]


def build_association_user_prompt(topic: str, context: str) -> str:
    if context:
        return f"トピック: {topic}\n背景: {context}"
    return f"トピック: {topic}"


def _strip_code_fences(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = [
        line for line in stripped.splitlines() if not line.strip().startswith("```")
    ]
    return "\n".join(lines).strip()


def _coerce_dict(value: _JsonValue) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _coerce_str(value: _JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _coerce_cat(value: _JsonValue) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return "?"


def _coerce_list(value: _JsonValue) -> list[object]:
    return value if isinstance(value, list) else []


type _CoercedStr = Annotated[str, BeforeValidator(_coerce_str)]
type _CoercedCat = Annotated[str, BeforeValidator(_coerce_cat)]


class _L3Node(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    term: _CoercedStr = ""
    cat: _CoercedCat = "?"
    hint: _CoercedStr = ""


class _L2Node(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    term: _CoercedStr = ""
    cat: _CoercedCat = "?"
    l3: Annotated[
        list[Annotated[_L3Node, BeforeValidator(_coerce_dict)]],
        BeforeValidator(_coerce_list),
    ] = []


class _L1Node(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    l1: _CoercedStr = ""
    cat: _CoercedCat = "?"
    l2: Annotated[
        list[Annotated[_L2Node, BeforeValidator(_coerce_dict)]],
        BeforeValidator(_coerce_list),
    ] = []


class _TreeModel(BaseModel):
    branches: Annotated[
        list[Annotated[_L1Node, BeforeValidator(_coerce_dict)]],
        BeforeValidator(_coerce_list),
    ] = []


def parse_association_tree(
    raw: str,
    *,
    root: str,
) -> Result[AssociationTree, AssociationTreeParseError]:
    cleaned = _strip_code_fences(raw)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return Err(
            AssociationTreeParseError(
                type=AssociationTreeParseErrorType.MISSING_JSON,
                message="model output contained no JSON object",
            )
        )
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        return Err(
            AssociationTreeParseError(
                type=AssociationTreeParseErrorType.INVALID_JSON,
                message=f"model output was not valid JSON: {exc.msg}",
            )
        )

    tree = _TreeModel.model_validate(parsed)

    summary_lines = [f"root: {root}"]
    l3_ideas: list[str] = []
    l2_ideas: list[str] = []
    l1_ideas: list[str] = []
    for branch in tree.branches:
        if not branch.l1:
            continue
        summary_lines.append(f"- {branch.l1} [{branch.cat}]")
        l1_ideas.append(f"{branch.l1}（{branch.cat}）")
        for l2 in branch.l2:
            if not l2.term:
                continue
            summary_lines.append(f"  - {l2.term} [{l2.cat}]")
            l2_ideas.append(f"{l2.term}（{l2.cat}）")
            for l3 in l2.l3:
                if not l3.term:
                    continue
                leaf = f"    - {l3.term} [{l3.cat}]"
                summary_lines.append(f"{leaf} {l3.hint}" if l3.hint else leaf)
                idea = f"{l3.term}（{l3.cat}）"
                l3_ideas.append(f"{idea}: {l3.hint}" if l3.hint else idea)

    if len(summary_lines) == 1:
        return Err(
            AssociationTreeParseError(
                type=AssociationTreeParseErrorType.EMPTY_TREE,
                message="model output contained no valid branches",
            )
        )

    ideas = l3_ideas or l2_ideas or l1_ideas
    return Ok(
        AssociationTree(
            root=root,
            tree_summary="\n".join(summary_lines),
            lateral_ideas=tuple(ideas),
        )
    )
