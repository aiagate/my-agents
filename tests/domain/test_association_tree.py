import json

import pytest
from flow_res import is_ok

from my_agents.domain.association_tree import (
    AssociationTree,
    AssociationTreeParseError,
    AssociationTreeParseErrorType,
    build_association_user_prompt,
    parse_association_tree,
)
from tests.helpers import assert_err


def test_build_association_user_prompt_with_and_without_context() -> None:
    assert build_association_user_prompt("コースター", "") == "トピック: コースター"
    assert (
        build_association_user_prompt("コースター", "賃貸1LDK")
        == "トピック: コースター\n背景: 賃貸1LDK"
    )


def test_parse_association_tree_parses_full_tree_and_hints() -> None:
    payload = {
        "root": "ignored",
        "branches": [
            {
                "l1": "コルク",
                "cat": "素材",
                "l2": [
                    {
                        "term": "板材",
                        "cat": "DIY",
                        "l3": [
                            {
                                "term": "断熱",
                                "cat": "機能",
                                "hint": "窓枠の隙間対策にも転用できる",
                            },
                            {
                                "term": "掲示板",
                                "cat": "文具",
                                "hint": "",
                            },
                        ],
                    }
                ],
            },
            {
                "l1": "皿",
                "cat": "形状",
                "l2": [
                    {
                        "term": "受け皿",
                        "cat": "園芸",
                        "l3": [
                            {
                                "term": "トレイ",
                                "cat": "収納",
                                "hint": "玄関の鍵置きにも応用できる",
                            }
                        ],
                    }
                ],
            },
        ],
    }

    result = parse_association_tree(
        json.dumps(payload, ensure_ascii=False),
        root="安いコースター",
    )

    assert is_ok(result)
    assert result.unwrap() == AssociationTree(
        root="安いコースター",
        tree_summary=(
            "root: 安いコースター\n"
            "- コルク [素材]\n"
            "  - 板材 [DIY]\n"
            "    - 断熱 [機能] 窓枠の隙間対策にも転用できる\n"
            "    - 掲示板 [文具]\n"
            "- 皿 [形状]\n"
            "  - 受け皿 [園芸]\n"
            "    - トレイ [収納] 玄関の鍵置きにも応用できる"
        ),
        lateral_ideas=(
            "断熱（機能）: 窓枠の隙間対策にも転用できる",
            "掲示板（文具）",
            "トレイ（収納）: 玄関の鍵置きにも応用できる",
        ),
    )


def test_parse_association_tree_strips_code_fences_and_prose() -> None:
    raw = (
        '```json\n前置きの説明 {"root": "x", "branches": [{"l1": "紙", "cat": "素材", '
        '"l2": [{"term": "封筒", "cat": "文具", "l3": []}]}]} 後書き\n```'
    )

    result = parse_association_tree(raw, root="メモ")

    assert is_ok(result)
    assert result.unwrap() == AssociationTree(
        root="メモ",
        tree_summary="root: メモ\n- 紙 [素材]\n  - 封筒 [文具]",
        lateral_ideas=("封筒（文具）",),
    )


def test_parse_association_tree_falls_back_to_l1_and_coerces_malformed_shapes() -> None:
    raw = json.dumps(
        {
            "branches": [
                "not-a-dict",
                {"l1": "", "cat": "空", "l2": []},
                {
                    "l1": "木",
                    "cat": "   ",
                    "l2": [
                        "bad-l2",
                        {"term": "", "cat": "無"},
                        {
                            "term": 123,
                            "l3": ["bad-l3", {"term": "", "hint": "skip"}],
                        },
                    ],
                },
                {"l1": "布", "cat": 99, "l2": "not-a-list"},
            ]
        },
        ensure_ascii=False,
    )

    result = parse_association_tree(raw, root="端材")

    assert is_ok(result)
    assert result.unwrap() == AssociationTree(
        root="端材",
        tree_summary="root: 端材\n- 木 [?]\n- 布 [?]",
        lateral_ideas=("木（?）", "布（?）"),
    )


def test_parse_association_tree_skips_bad_l3_items_and_uses_l2_fallback() -> None:
    raw = json.dumps(
        {
            "branches": [
                {
                    "l1": "鉄",
                    "cat": "素材",
                    "l2": [
                        {
                            "term": "磁石",
                            "cat": "物理",
                            "l3": "not-a-list",
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    result = parse_association_tree(raw, root="フック")

    assert is_ok(result)
    assert result.unwrap() == AssociationTree(
        root="フック",
        tree_summary="root: フック\n- 鉄 [素材]\n  - 磁石 [物理]",
        lateral_ideas=("磁石（物理）",),
    )


def test_parse_association_tree_skips_empty_l3_term() -> None:
    raw = json.dumps(
        {
            "branches": [
                {
                    "l1": "鉄",
                    "cat": "素材",
                    "l2": [
                        {
                            "term": "磁石",
                            "cat": "物理",
                            "l3": [{"term": "  ", "cat": "無", "hint": "ignored"}],
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    result = parse_association_tree(raw, root="フック")

    assert is_ok(result)
    assert result.unwrap().lateral_ideas == ("磁石（物理）",)


@pytest.mark.parametrize(
    ("raw", "expected_type", "message_contains"),
    [
        pytest.param(
            "plain text only",
            AssociationTreeParseErrorType.MISSING_JSON,
            "contained no JSON object",
            id="no-braces",
        ),
        pytest.param(
            "} reversed {",
            AssociationTreeParseErrorType.MISSING_JSON,
            "contained no JSON object",
            id="reversed-braces",
        ),
        pytest.param(
            "{not valid json}",
            AssociationTreeParseErrorType.INVALID_JSON,
            "not valid JSON",
            id="invalid-json",
        ),
        pytest.param(
            '{"branches": "invalid"}',
            AssociationTreeParseErrorType.EMPTY_TREE,
            "no valid branches",
            id="non-list-branches",
        ),
        pytest.param(
            '{"branches": [{"l1": "", "cat": "素材"}]}',
            AssociationTreeParseErrorType.EMPTY_TREE,
            "no valid branches",
            id="empty-l1",
        ),
    ],
)
def test_parse_association_tree_errors(
    raw: str,
    expected_type: AssociationTreeParseErrorType,
    message_contains: str,
) -> None:
    result = parse_association_tree(raw, root="コースター")

    error = assert_err(
        result,
        expected_type=expected_type,
        message_contains=message_contains,
    )
    assert isinstance(error, AssociationTreeParseError)
