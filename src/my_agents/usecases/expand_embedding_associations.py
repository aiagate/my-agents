from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.association_expander import (
    AssociationExpandError,
    AssociationExpandErrorType,
    AssociationExpandOutcome,
    AssociationExpandRequest,
    IAssociationExpander,
)
from my_agents.contracts.ports.embedding_client import (
    EmbeddingClientError,
    IEmbeddingClient,
)
from my_agents.domain.concept_lexicon import DEFAULT_CONCEPT_LEXICON, ConceptEntry
from my_agents.domain.embedding_associator import build_magical_banana_tree


def _to_embedding_error(error: EmbeddingClientError) -> AssociationExpandError:
    return AssociationExpandError(
        type=AssociationExpandErrorType.EMBEDDING_ERROR,
        message=error.message,
    )


class ExpandEmbeddingAssociationsHandler(IAssociationExpander):
    def __init__(
        self,
        embedding_client: IEmbeddingClient,
        lexicon: tuple[ConceptEntry, ...] = DEFAULT_CONCEPT_LEXICON,
    ) -> None:
        self._embedding_client = embedding_client
        self._lexicon = lexicon
        self._cached_vectors: tuple[tuple[float, ...], ...] | None = None

    async def expand(
        self,
        request: AssociationExpandRequest,
    ) -> Result[AssociationExpandOutcome, AssociationExpandError]:
        topic = request.topic.strip()
        context = request.context.strip()
        if not topic:
            return Err(
                AssociationExpandError(
                    type=AssociationExpandErrorType.INVALID_INPUT,
                    message="Topic must not be empty.",
                )
            )

        query_text = f"{topic} {context}" if context else topic
        query_result = await self._embedding_client.embed((query_text,))
        if is_err(query_result):
            return Err(_to_embedding_error(query_result.error))
        query_vector = query_result.value[0]

        if self._cached_vectors is None:
            lexicon_result = await self._embedding_client.embed(
                tuple(entry.to_embedding_text() for entry in self._lexicon)
            )
            if is_err(lexicon_result):
                return Err(_to_embedding_error(lexicon_result.error))
            self._cached_vectors = lexicon_result.value

        tree = build_magical_banana_tree(
            topic,
            query_vector,
            self._lexicon,
            self._cached_vectors,
        )
        return Ok(
            AssociationExpandOutcome(
                root=tree.root,
                tree_summary=tree.tree_summary,
                lateral_ideas=tree.lateral_ideas,
            )
        )
