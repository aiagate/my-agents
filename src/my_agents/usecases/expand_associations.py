from typing import assert_never

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.association_expander import (
    AssociationExpandError,
    AssociationExpandErrorType,
    AssociationExpandOutcome,
    AssociationExpandRequest,
    IAssociationExpander,
)
from my_agents.contracts.ports.association_generator import (
    AssociationGeneratorError,
    AssociationGeneratorErrorType,
    IAssociationGenerator,
)
from my_agents.domain.association_tree import (
    ASSOCIATION_SYSTEM_PROMPT,
    build_association_user_prompt,
    parse_association_tree,
)


def _to_expand_error(error: AssociationGeneratorError) -> AssociationExpandError:
    match error.type:
        case (
            AssociationGeneratorErrorType.MODEL_UNAVAILABLE
            | AssociationGeneratorErrorType.GENERATION_FAILED
        ):
            return AssociationExpandError(
                type=AssociationExpandErrorType.GENERATION_ERROR,
                message=error.message,
            )
        case _:
            assert_never(error.type)


class ExpandAssociationsHandler(IAssociationExpander):
    def __init__(self, generator: IAssociationGenerator) -> None:
        self._generator = generator

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

        gen_result = await self._generator.generate(
            ASSOCIATION_SYSTEM_PROMPT,
            build_association_user_prompt(topic, context),
        )
        if is_err(gen_result):
            return Err(_to_expand_error(gen_result.error))

        parsed = parse_association_tree(gen_result.value, root=topic)
        if is_err(parsed):
            return Err(
                AssociationExpandError(
                    type=AssociationExpandErrorType.GENERATION_ERROR,
                    message=parsed.error.message,
                )
            )

        tree = parsed.value
        return Ok(
            AssociationExpandOutcome(
                root=tree.root,
                tree_summary=tree.tree_summary,
                lateral_ideas=tree.lateral_ideas,
            )
        )
