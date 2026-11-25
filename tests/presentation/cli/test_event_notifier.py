from my_agents.contracts.ports.event_notifier import EventNotification
from my_agents.contracts.ports.logger import IAppLogger, LogEntry, LogLevel
from my_agents.presentation.cli.event_notifier import CliEventNotifier


class RecordingLogger(IAppLogger):
    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def log(self, entry: LogEntry, /) -> None:
        self.entries.append(entry)


def test_notifies_completed_event_to_main_chat_output() -> None:
    outputs: list[str] = []
    logger = RecordingLogger()
    notifier = CliEventNotifier(outputs.append, logger=logger)

    notifier.notify(EventNotification("evt_one", "analysis body", failed=False))

    assert outputs == ["\nanalysis body"]
    assert logger.entries[0] == LogEntry(
        level=LogLevel.INFO,
        event="event_completed",
        message="evt_one completed",
    )


def test_notifies_failed_event_at_error_level() -> None:
    outputs: list[str] = []
    logger = RecordingLogger()
    notifier = CliEventNotifier(outputs.append, logger=logger)

    notifier.notify(EventNotification("evt_two", "failure body", failed=True))

    assert outputs == ["\nfailure body"]
    assert logger.entries[0].level is LogLevel.ERROR
    assert logger.entries[0].event == "event_failed"
    assert logger.entries[0].message == "evt_two failed"
