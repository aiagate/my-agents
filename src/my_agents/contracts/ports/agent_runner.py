from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto

from flow_res import Result

from my_agents.contracts.messages import AgentReply, ChatMessage


class AgentRunnerErrorType(Enum):
    API_ERROR = auto()
    UNEXPECTED_BEHAVIOR = auto()
    USAGE_LIMIT_EXCEEDED = auto()
    UNEXPECTED = auto()


@dataclass(frozen=True, slots=True)
class AgentRunnerError:
    type: AgentRunnerErrorType
    message: str
    detail: str | None = None


class IAgentRunner(ABC):
    @abstractmethod
    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]: ...
