from collections.abc import Callable

from my_agents.contracts.ports.event_notifier import (
    EventNotification,
    IEventNotifier,
)
from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
)


class CliEventNotifier(IEventNotifier):
    def __init__(
        self,
        write_output: Callable[[str], None],
        *,
        logger: IAppLogger = NOOP_LOGGER,
    ) -> None:
        self._write_output = write_output
        self._logger = logger

    def notify(self, notification: EventNotification, /) -> None:
        status = "failed" if notification.failed else "completed"
        self._logger.log(
            LogEntry(
                level=LogLevel.ERROR if notification.failed else LogLevel.INFO,
                event="event_completed" if not notification.failed else "event_failed",
                message=f"{notification.event_id} {status}",
            )
        )
        self._write_output(f"\n{notification.content}")
