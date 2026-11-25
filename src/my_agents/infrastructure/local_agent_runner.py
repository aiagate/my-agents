import re
from collections.abc import Sequence

from flow_res import Ok, Result

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import AgentRunnerError, IAgentRunner

_EVENT_ID_PATTERN = re.compile(r"\[event:(evt_[0-9a-zA-Z_]+)\]")


class LocalAgentRunner(IAgentRunner):
    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]:
        _ = instructions
        event_ids = tuple(
            match.group(1)
            for message in history
            for match in _EVENT_ID_PATTERN.finditer(message.content)
        )
        lowered = user_input.lower()
        if "イベント" in user_input or "event" in lowered:
            if event_ids:
                return Ok(
                    AgentReply(
                        content=(
                            "ローカル応答: 直近のイベントは "
                            f"{event_ids[-1]} です。会話履歴から参照しました。"
                        )
                    )
                )
            return Ok(AgentReply(content="ローカル応答: イベント履歴はありません。"))
        return Ok(AgentReply(content=f"ローカル応答: {user_input}"))
