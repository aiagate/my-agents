import json

import pytest
from flow_res import Err, Ok, Result, is_ok

from my_agents.contracts.ports.association_expander import (
    AssociationExpandError,
    AssociationExpandErrorType,
    AssociationExpandRequest,
)
from my_agents.contracts.ports.association_generator import (
    AssociationGeneratorError,
    AssociationGeneratorErrorType,
    IAssociationGenerator,
)
from my_agents.domain.association_tree import ASSOCIATION_SYSTEM_PROMPT
from my_agents.usecases.expand_associations import ExpandAssociationsHandler
from tests.helpers import assert_err


class FakeAssociationGenerator(IAssociationGenerator):
    def __init__(
        self,
        result: Result[str, AssociationGeneratorError],
    ) -> None:
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Result[str, AssociationGeneratorError]:
        self.calls.append((system_prompt, user_prompt))
        return self._result


def _valid_tree_json() -> str:
    return json.dumps(
        {
            "root": "固定費の見直し",
            "branches": [
                {
                    "l1": "電気代",
                    "cat": "お金",
                    "l2": [
                        {
                            "term": "省電力",
                            "cat": "機能",
                            "l3": [
                                {
                                    "term": "自動遮断",
                                    "cat": "仕組み",
                                    "hint": "契約時に自動解約条件を埋め込む",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.anyio
async def test_expands_associations_with_and_without_context() -> None:
    generator = FakeAssociationGenerator(Ok(_valid_tree_json()))
    handler = ExpandAssociationsHandler(generator)

    with_context = await handler.expand(
        AssociationExpandRequest(topic="  固定費の見直し ", context=" 月次管理 ")
    )
    without_context = await handler.expand(
        AssociationExpandRequest(topic="コースター", context="   ")
    )

    assert is_ok(with_context)
    assert with_context.unwrap().root == "固定費の見直し"
    assert with_context.unwrap().tree_summary == (
        "root: 固定費の見直し\n"
        "- 電気代 [お金]\n"
        "  - 省電力 [機能]\n"
        "    - 自動遮断 [仕組み] 契約時に自動解約条件を埋め込む"
    )
    assert with_context.unwrap().lateral_ideas == (
        "自動遮断（仕組み）: 契約時に自動解約条件を埋め込む",
    )
    assert is_ok(without_context)
    assert generator.calls == [
        (ASSOCIATION_SYSTEM_PROMPT, "トピック: 固定費の見直し\n背景: 月次管理"),
        (ASSOCIATION_SYSTEM_PROMPT, "トピック: コースター"),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("topic", ["", "   ", "\n\t"])
async def test_rejects_blank_topic_without_calling_generator(topic: str) -> None:
    generator = FakeAssociationGenerator(Ok(_valid_tree_json()))
    handler = ExpandAssociationsHandler(generator)

    result = await handler.expand(AssociationExpandRequest(topic=topic))

    error = assert_err(
        result,
        expected_type=AssociationExpandErrorType.INVALID_INPUT,
        message_contains="Topic must not be empty.",
    )
    assert isinstance(error, AssociationExpandError)
    assert generator.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        pytest.param(
            AssociationGeneratorErrorType.MODEL_UNAVAILABLE,
            "adapter missing",
            id="model-unavailable",
        ),
        pytest.param(
            AssociationGeneratorErrorType.GENERATION_FAILED,
            "inference crashed",
            id="generation-failed",
        ),
    ],
)
async def test_maps_generator_errors_to_generation_error(
    error_type: AssociationGeneratorErrorType,
    message: str,
) -> None:
    generator = FakeAssociationGenerator(
        Err(AssociationGeneratorError(type=error_type, message=message))
    )
    handler = ExpandAssociationsHandler(generator)

    result = await handler.expand(AssociationExpandRequest(topic="キャリア"))

    error = assert_err(
        result,
        expected_type=AssociationExpandErrorType.GENERATION_ERROR,
        message_contains=message,
    )
    assert isinstance(error, AssociationExpandError)


@pytest.mark.anyio
async def test_maps_malformed_model_output_to_generation_error() -> None:
    generator = FakeAssociationGenerator(Ok("no json payload"))
    handler = ExpandAssociationsHandler(generator)

    result = await handler.expand(AssociationExpandRequest(topic="キャリア"))

    assert_err(
        result,
        expected_type=AssociationExpandErrorType.GENERATION_ERROR,
        message_contains="contained no JSON object",
    )
