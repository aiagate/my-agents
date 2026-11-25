import re
from collections.abc import Sequence

from flow_res import Ok, Result

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import AgentRunnerError, IAgentRunner
from my_agents.domain.event_context import EVENT_SECTION_HEADER

_EVENT_ID_PATTERN = re.compile(r"\[event:(evt_[0-9a-zA-Z_]+)\]")


def _event_ids(instructions: str) -> tuple[str, ...]:
    _, marker, section = instructions.partition(EVENT_SECTION_HEADER)
    if not marker:
        return ()
    return tuple(match.group(1) for match in _EVENT_ID_PATTERN.finditer(section))


class LocalAgentRunner(IAgentRunner):
    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]:
        _ = history
        event_ids = _event_ids(instructions)
        lowered = user_input.lower()
        if "イベント" in user_input or "event" in lowered:
            if event_ids:
                return Ok(
                    AgentReply(
                        content=(
                            "ローカル応答: 参照できるイベントは "
                            f"{event_ids[0]} です。イベント文脈から参照しました。"
                        )
                    )
                )
            return Ok(
                AgentReply(content="ローカル応答: イベント文脈にイベントはありません。")
            )
        return Ok(AgentReply(content=f"ローカル応答: {user_input}"))
