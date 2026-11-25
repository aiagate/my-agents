from collections.abc import Sequence

import pytest
from flow_res import is_ok

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.infrastructure.local_agent_runner import LocalAgentRunner


@pytest.mark.anyio
async def test_local_runner_echoes_normal_input() -> None:
    result = await LocalAgentRunner().run(
        "こんにちは", history=(), instructions="ignored"
    )

    assert is_ok(result)
    assert result.value == AgentReply(content="ローカル応答: こんにちは")


@pytest.mark.anyio
async def test_local_runner_references_latest_event_from_history() -> None:
    history = (
        ChatMessage(role="assistant", content="[event:evt_old] completed"),
        ChatMessage(role="assistant", content="[event:evt_new] completed"),
    )

    result = await LocalAgentRunner().run(
        "直近のイベントは？", history=history, instructions="ignored"
    )

    assert is_ok(result)
    assert result.value.content == (
        "ローカル応答: 直近のイベントは evt_new です。会話履歴から参照しました。"
    )


@pytest.mark.anyio
async def test_local_runner_reports_missing_event_history() -> None:
    history: Sequence[ChatMessage] = (
        ChatMessage(role="user", content="イベントについて教えて"),
    )

    result = await LocalAgentRunner().run(
        "event は？", history=history, instructions="ignored"
    )

    assert is_ok(result)
    assert result.value.content == "ローカル応答: イベント履歴はありません。"
