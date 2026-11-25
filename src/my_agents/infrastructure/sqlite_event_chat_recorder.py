from datetime import datetime

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.chat_history import (
    IChatStore,
    NewDialogueTurn,
)
from my_agents.contracts.ports.event_chat import (
    EventChatRecordError,
    IEventChatRecorder,
)


class SqliteEventChatRecorder(IEventChatRecorder):
    def __init__(self, chat_store: IChatStore) -> None:
        self._chat_store = chat_store

    def record(
        self, event_id: str, content: str, *, occurred_at: datetime
    ) -> Result[None, EventChatRecordError]:
        result = self._chat_store.record_turns(
            None,
            (NewDialogueTurn("assistant", content, occurred_at),),
            title=f"イベント {event_id}",
            updated_at=occurred_at,
        )
        if is_err(result):
            return Err(EventChatRecordError(result.error.message))
        return Ok(None)
