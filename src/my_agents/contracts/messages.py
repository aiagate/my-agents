from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class AgentReply:
    content: str
    core_memory_updates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatWarning:
    event: str
    error_type: str
    message: str
    display_message: str
    detail: str | None = None
