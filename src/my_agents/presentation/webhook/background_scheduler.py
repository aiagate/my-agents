import time
from queue import Queue
from threading import Thread

from flow_res import is_err

from my_agents.contracts.events import InboundEvent
from my_agents.contracts.ports.event_scheduler import IEventScheduler
from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
)
from my_agents.usecases.handle_event import HandleEventUseCase


class BackgroundEventScheduler(IEventScheduler):
    def __init__(
        self,
        handle_event: HandleEventUseCase,
        *,
        delay_ms: int = 0,
        logger: IAppLogger = NOOP_LOGGER,
    ) -> None:
        self._handle_event = handle_event
        self._delay_ms = delay_ms
        self._logger = logger
        self._queue: Queue[InboundEvent | None] = Queue()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="event-worker", daemon=True)
        self._thread.start()

    def schedule(self, event: InboundEvent, /) -> None:
        self._queue.put(event)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join()
        self._thread = None

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            if self._delay_ms > 0:
                time.sleep(self._delay_ms / 1000)
            try:
                result = self._handle_event.execute(event)
            except Exception as exc:
                self._logger.log(
                    LogEntry(
                        level=LogLevel.ERROR,
                        event="event_worker_crashed",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue
            if is_err(result):
                self._logger.log(
                    LogEntry(
                        level=LogLevel.WARNING,
                        event="event_worker_failed",
                        error_type=result.error.type.name,
                        message=result.error.message,
                    )
                )
