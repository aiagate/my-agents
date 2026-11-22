import pytest

from my_agents.domain.concept_lexicon import DEFAULT_CONCEPT_LEXICON, ConceptEntry
from my_agents.domain.embedding_associator import (
    AssociationTree,
    build_magical_banana_tree,
    cosine_similarity,
)


def _entry(term: str, category: str, description: str = "") -> ConceptEntry:
    return ConceptEntry(
        term=term,
        category=category,
        description=description or f"{term}の説明",
    )


def test_concept_entry_builds_embedding_text() -> None:
    entry = ConceptEntry(
        term="珪藻土",
        category="素材・テクスチャ",
        description="水分を素早く吸収する。",
    )

    assert entry.to_embedding_text() == (
        "珪藻土（素材・テクスチャ）: 水分を素早く吸収する。"
    )


def test_default_lexicon_is_structurally_well_formed() -> None:
    categories = {entry.category for entry in DEFAULT_CONCEPT_LEXICON}
    terms = [entry.term for entry in DEFAULT_CONCEPT_LEXICON]

    assert terms
    assert len(categories) > 1
    assert len(terms) == len(set(terms))
    assert all(entry.description.strip() for entry in DEFAULT_CONCEPT_LEXICON)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        pytest.param((1.0, 0.0), (1.0, 0.0), 1.0, id="identical"),
        pytest.param((1.0, 0.0), (0.0, 1.0), 0.0, id="orthogonal"),
        pytest.param((1.0, 0.0), (-1.0, 0.0), -1.0, id="opposite"),
        pytest.param((), (1.0, 0.0), 0.0, id="empty-left"),
        pytest.param((1.0, 0.0), (), 0.0, id="empty-right"),
        pytest.param((), (), 0.0, id="empty-both"),
        pytest.param((1.0, 0.0), (1.0,), 0.0, id="mismatched-length"),
        pytest.param((0.0, 0.0), (1.0, 0.0), 0.0, id="zero-left-norm"),
        pytest.param((1.0, 0.0), (0.0, 0.0), 0.0, id="zero-right-norm"),
    ],
)
def test_cosine_similarity(
    left: tuple[float, ...],
    right: tuple[float, ...],
    expected: float,
) -> None:
    assert cosine_similarity(left, right) == pytest.approx(expected)


def test_cosine_similarity_normalizes_scaled_vectors() -> None:
    result = cosine_similarity((3.0, 4.0), (6.0, 8.0))

    assert result == pytest.approx(1.0)


def _diverse_lexicon() -> tuple[ConceptEntry, ...]:
    return (
        _entry("a1", "A"),
        _entry("a2", "A"),
        _entry("b1", "B"),
        _entry("b2", "B"),
        _entry("c1", "C"),
        _entry("c2", "C"),
        _entry("d1", "D"),
        _entry("d2", "D"),
        _entry("e1", "E"),
        _entry("e2", "E"),
    )


def _diverse_vectors() -> tuple[tuple[float, ...], ...]:
    return (
        (1.00, 0.00),
        (0.98, 0.20),
        (0.95, 0.31),
        (0.90, 0.44),
        (0.85, 0.53),
        (0.80, 0.60),
        (0.75, 0.66),
        (0.70, 0.71),
        (0.65, 0.76),
        (0.60, 0.80),
    )


def test_builds_cross_category_three_level_tree() -> None:
    outcome = build_magical_banana_tree(
        "引っ越しの棚",
        (1.0, 0.0),
        _diverse_lexicon(),
        _diverse_vectors(),
    )

    lines = outcome.tree_summary.splitlines()
    assert isinstance(outcome, AssociationTree)
    assert outcome.root == "引っ越しの棚"
    assert lines[0] == "root: 引っ越しの棚"
    assert lines[1] == "- L1 a1 [A] (1.000)"
    assert lines[2].startswith("  - L2 b2 [B] (")
    assert lines[3].startswith("    - L3 c2 [C] (")
    assert lines[4].startswith("    - L3 d1 [D] (")
    assert lines[5].startswith("  - L2 c1 [C] (")
    assert lines[6].startswith("    - L3 d2 [D] (")
    assert lines[7].startswith("    - L3 e1 [E] (")
    assert lines[8].startswith("- L1 b1 [B] (")
    assert lines[9].startswith("  - L2 a2 [A] (")
    assert lines[10].startswith("  - L2 e2 [E] (")
    assert len(lines) == 11
    assert outcome.lateral_ideas == (
        "b2（B: b2の説明）",
        "c2（C: c2の説明）",
        "d1（D: d1の説明）",
        "c1（C: c1の説明）",
        "d2（D: d2の説明）",
        "e1（E: e1の説明）",
        "a2（A: a2の説明）",
        "e2（E: e2の説明）",
    )


def test_uses_same_category_preferred_fallback() -> None:
    lexicon = (
        _entry("a", "A"),
        _entry("b1", "B"),
        _entry("c1", "C"),
        _entry("a2", "A"),
        _entry("c2", "C"),
        _entry("c3", "C"),
    )
    vectors = (
        (1.00, 0.00),
        (0.90, 0.10),
        (0.80, 0.20),
        (0.70, 0.30),
        (0.60, 0.40),
        (0.50, 0.50),
    )

    outcome = build_magical_banana_tree("root", (1.0, 0.0), lexicon, vectors)

    lines = outcome.tree_summary.splitlines()
    assert lines[0] == "root: root"
    assert lines[1].startswith("- L1 a [A] (")
    assert lines[2].startswith("  - L2 c1 [C] (")
    assert lines[3].startswith("  - L2 c2 [C] (")
    assert lines[4].startswith("- L1 b1 [B] (")
    assert lines[5].startswith("  - L2 a2 [A] (")
    assert lines[6].startswith("  - L2 c3 [C] (")
    assert len(lines) == 7
    assert outcome.lateral_ideas == (
        "c1（C: c1の説明）",
        "c2（C: c2の説明）",
        "a2（A: a2の説明）",
        "c3（C: c3の説明）",
    )


def test_empty_lexicon_returns_root_only_tree() -> None:
    outcome = build_magical_banana_tree("root", (1.0, 0.0), (), ())

    assert outcome == AssociationTree(
        root="root",
        tree_summary="root: root",
        lateral_ideas=(),
    )
