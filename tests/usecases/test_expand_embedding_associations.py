from collections.abc import Callable, Sequence

import pytest
from flow_res import Err, Ok, Result, is_ok

from my_agents.contracts.ports.association_expander import (
    AssociationExpandError,
    AssociationExpandErrorType,
    AssociationExpandRequest,
)
from my_agents.contracts.ports.embedding_client import (
    EmbeddingClientError,
    EmbeddingClientErrorType,
    IEmbeddingClient,
)
from my_agents.domain.concept_lexicon import DEFAULT_CONCEPT_LEXICON, ConceptEntry
from my_agents.usecases.expand_embedding_associations import (
    ExpandEmbeddingAssociationsHandler,
)
from tests.helpers import assert_err

_VECTORS: tuple[tuple[float, ...], ...] = (
    (1.0, 0.0),
    (0.8, 0.2),
    (0.6, 0.4),
)
_LEXICON: tuple[ConceptEntry, ...] = (
    ConceptEntry(term="a", category="A", description="aの説明"),
    ConceptEntry(term="b", category="B", description="bの説明"),
    ConceptEntry(term="c", category="C", description="cの説明"),
)


class FakeEmbeddingClient(IEmbeddingClient):
    def __init__(
        self,
        results: Sequence[Result[tuple[tuple[float, ...], ...], EmbeddingClientError]],
    ) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []

    async def embed(
        self,
        texts: Sequence[str],
    ) -> Result[tuple[tuple[float, ...], ...], EmbeddingClientError]:
        self.calls.append(tuple(texts))
        return self._results[len(self.calls) - 1]


class CallbackEmbeddingClient(IEmbeddingClient):
    def __init__(
        self,
        callback: Callable[[tuple[str, ...]], tuple[tuple[float, ...], ...]],
    ) -> None:
        self._callback = callback
        self.calls: list[tuple[str, ...]] = []

    async def embed(
        self,
        texts: Sequence[str],
    ) -> Result[tuple[tuple[float, ...], ...], EmbeddingClientError]:
        recorded = tuple(texts)
        self.calls.append(recorded)
        return Ok(self._callback(recorded))


def _embedding_error(message: str = "upstream failed") -> EmbeddingClientError:
    return EmbeddingClientError(
        type=EmbeddingClientErrorType.API_ERROR,
        message=message,
    )


@pytest.mark.anyio
async def test_expands_with_context_and_caches_lexicon_vectors() -> None:
    client = FakeEmbeddingClient(
        [
            Ok(((1.0, 0.0),)),
            Ok(_VECTORS),
            Ok(((1.0, 0.0),)),
        ]
    )
    handler = ExpandEmbeddingAssociationsHandler(client, _LEXICON)

    first = await handler.expand(
        AssociationExpandRequest(topic="コースター", context="1LDK")
    )
    second = await handler.expand(AssociationExpandRequest(topic="コースター"))

    assert is_ok(first)
    assert first.unwrap().root == "コースター"
    assert first.unwrap().tree_summary.splitlines() == [
        "root: コースター",
        "- L1 a [A] (1.000)",
        "  - L2 c [C] (0.832)",
        "- L1 b [B] (0.970)",
    ]
    assert first.unwrap().lateral_ideas == ("c（C: cの説明）",)
    assert is_ok(second)
    assert len(client.calls) == 3
    assert client.calls[0] == ("コースター 1LDK",)
    assert client.calls[1] == (
        "a（A）: aの説明",
        "b（B）: bの説明",
        "c（C）: cの説明",
    )
    assert client.calls[2] == ("コースター",)


@pytest.mark.anyio
async def test_uses_default_lexicon_when_none_is_supplied() -> None:
    client = CallbackEmbeddingClient(lambda texts: tuple((1.0, 0.0) for _text in texts))
    handler = ExpandEmbeddingAssociationsHandler(client)

    result = await handler.expand(AssociationExpandRequest(topic="日常"))

    assert is_ok(result)
    assert len(client.calls) == 2
    assert len(client.calls[1]) == len(DEFAULT_CONCEPT_LEXICON)
    assert client.calls[1][0] == DEFAULT_CONCEPT_LEXICON[0].to_embedding_text()
    assert result.unwrap().tree_summary.startswith("root: 日常")


@pytest.mark.anyio
@pytest.mark.parametrize("topic", ["", "   ", "\n\t"])
async def test_rejects_blank_topic_without_embedding(topic: str) -> None:
    client = FakeEmbeddingClient([])
    handler = ExpandEmbeddingAssociationsHandler(client, _LEXICON)

    result = await handler.expand(AssociationExpandRequest(topic=topic))

    error = assert_err(
        result,
        expected_type=AssociationExpandErrorType.INVALID_INPUT,
        message_contains="Topic must not be empty.",
    )
    assert isinstance(error, AssociationExpandError)
    assert client.calls == []


@pytest.mark.anyio
async def test_maps_query_embedding_error() -> None:
    client = FakeEmbeddingClient([Err(_embedding_error("query failed"))])
    handler = ExpandEmbeddingAssociationsHandler(client, _LEXICON)

    result = await handler.expand(AssociationExpandRequest(topic="topic"))

    error = assert_err(
        result,
        expected_type=AssociationExpandErrorType.EMBEDDING_ERROR,
        message_contains="query failed",
    )
    assert isinstance(error, AssociationExpandError)
    assert len(client.calls) == 1


@pytest.mark.anyio
async def test_maps_lexicon_embedding_error_and_retries_on_next_call() -> None:
    client = FakeEmbeddingClient(
        [
            Ok(((1.0, 0.0),)),
            Err(_embedding_error("lexicon failed")),
            Ok(((1.0, 0.0),)),
            Ok(_VECTORS),
        ]
    )
    handler = ExpandEmbeddingAssociationsHandler(client, _LEXICON)

    first = await handler.expand(AssociationExpandRequest(topic="first"))
    second = await handler.expand(AssociationExpandRequest(topic="second"))

    assert_err(
        first,
        expected_type=AssociationExpandErrorType.EMBEDDING_ERROR,
        message_contains="lexicon failed",
    )
    assert is_ok(second)
    assert len(client.calls) == 4
    assert len(client.calls[1]) == 3
    assert len(client.calls[3]) == 3


@pytest.mark.anyio
async def test_handles_empty_lexicon() -> None:
    client = FakeEmbeddingClient(
        [
            Ok(((1.0, 0.0),)),
            Ok(()),
        ]
    )
    handler = ExpandEmbeddingAssociationsHandler(client, ())

    result = await handler.expand(AssociationExpandRequest(topic="topic"))

    assert is_ok(result)
    assert result.unwrap().tree_summary == "root: topic"
    assert result.unwrap().lateral_ideas == ()
    assert client.calls == [("topic",), ()]
