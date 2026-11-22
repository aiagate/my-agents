import math
from collections.abc import Callable
from dataclasses import dataclass

from my_agents.domain.concept_lexicon import ConceptEntry

_BRANCH_FACTOR = 2


@dataclass(frozen=True, slots=True)
class AssociationTree:
    root: str
    tree_summary: str
    lateral_ideas: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    index: int
    score: float
    entry: ConceptEntry
    vector: tuple[float, ...]


def cosine_similarity(
    left: tuple[float, ...],
    right: tuple[float, ...],
) -> float:
    if len(left) != len(right):
        return 0.0
    dot = 0.0
    left_square = 0.0
    right_square = 0.0
    for left_value, right_value in zip(left, right, strict=False):
        dot += left_value * right_value
        left_square += left_value * left_value
        right_square += right_value * right_value
    if left_square == 0.0 or right_square == 0.0:
        return 0.0
    similarity = dot / (math.sqrt(left_square) * math.sqrt(right_square))
    return max(-1.0, min(1.0, similarity))


def _rank_candidates(
    lexicon: tuple[ConceptEntry, ...],
    lexicon_vectors: tuple[tuple[float, ...], ...],
    query_vector: tuple[float, ...],
    used: set[int],
) -> tuple[_Candidate, ...]:
    ranked: list[_Candidate] = []
    for index, (entry, vector) in enumerate(
        zip(lexicon, lexicon_vectors, strict=False)
    ):
        if index in used:
            continue
        ranked.append(
            _Candidate(
                index=index,
                score=cosine_similarity(query_vector, vector),
                entry=entry,
                vector=vector,
            )
        )
    ranked.sort(key=lambda candidate: candidate.score, reverse=True)
    return tuple(ranked)


def _choose_candidates(
    ranked: tuple[_Candidate, ...],
    used: set[int],
    count: int,
    *,
    is_preferred: Callable[[ConceptEntry], bool],
) -> tuple[_Candidate, ...]:
    chosen: list[_Candidate] = []
    chosen_categories: set[str] = set()

    for candidate in ranked:
        if len(chosen) >= count:
            return tuple(chosen)
        if candidate.index in used or not is_preferred(candidate.entry):
            continue
        if candidate.entry.category in chosen_categories:
            continue
        chosen.append(candidate)
        used.add(candidate.index)
        chosen_categories.add(candidate.entry.category)

    for candidate in ranked:
        if len(chosen) >= count:
            return tuple(chosen)
        if candidate.index in used or not is_preferred(candidate.entry):
            continue
        chosen.append(candidate)
        used.add(candidate.index)

    return tuple(chosen)


def _format_node(candidate: _Candidate) -> str:
    return (
        f"{candidate.entry.term} [{candidate.entry.category}] ({candidate.score:.3f})"
    )


def _format_lateral_idea(entry: ConceptEntry) -> str:
    return f"{entry.term}（{entry.category}: {entry.description}）"


def build_magical_banana_tree(
    root: str,
    query_vector: tuple[float, ...],
    lexicon: tuple[ConceptEntry, ...],
    lexicon_vectors: tuple[tuple[float, ...], ...],
) -> AssociationTree:
    used: set[int] = set()
    lines: list[str] = [f"root: {root}"]
    lateral_ideas: list[str] = []

    level1 = _choose_candidates(
        _rank_candidates(lexicon, lexicon_vectors, query_vector, used),
        used,
        _BRANCH_FACTOR,
        is_preferred=lambda _entry: True,
    )
    for node1 in level1:
        lines.append(f"- L1 {_format_node(node1)}")
        level2 = _choose_candidates(
            _rank_candidates(lexicon, lexicon_vectors, node1.vector, used),
            used,
            _BRANCH_FACTOR,
            is_preferred=lambda entry, parent=node1: (
                entry.category != parent.entry.category
            ),
        )
        for node2 in level2:
            lines.append(f"  - L2 {_format_node(node2)}")
            lateral_ideas.append(_format_lateral_idea(node2.entry))
            level3 = _choose_candidates(
                _rank_candidates(lexicon, lexicon_vectors, node2.vector, used),
                used,
                _BRANCH_FACTOR,
                is_preferred=lambda entry, parent=node2, ancestor=node1: (
                    entry.category
                    not in {parent.entry.category, ancestor.entry.category}
                ),
            )
            for node3 in level3:
                lines.append(f"    - L3 {_format_node(node3)}")
                lateral_ideas.append(_format_lateral_idea(node3.entry))

    return AssociationTree(
        root=root,
        tree_summary="\n".join(lines),
        lateral_ideas=tuple(lateral_ideas),
    )
