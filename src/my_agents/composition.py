import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import assert_never

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.agent_runner import IAgentRunner
from my_agents.contracts.ports.episode_summarizer import IEpisodeSummarizer
from my_agents.contracts.ports.event_webhook import IEventWebhookServer
from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
)
from my_agents.infrastructure.file_logger import JsonlFileLogger, resolve_log_path
from my_agents.infrastructure.local_agent_runner import LocalAgentRunner
from my_agents.infrastructure.local_episode_summarizer import LocalEpisodeSummarizer
from my_agents.infrastructure.markdown_core_profile_store import (
    MarkdownCoreProfileStore,
)
from my_agents.infrastructure.pydantic_ai_episode_summarizer import (
    PydanticAIEpisodeSummarizer,
)
from my_agents.infrastructure.pydantic_ai_runner import PydanticAIAgentRunner
from my_agents.infrastructure.settings import (
    AppSettings,
    SettingsError,
    load_app_settings,
)
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.infrastructure.sqlite_event_chat_recorder import (
    SqliteEventChatRecorder,
)
from my_agents.presentation.cli.args import (
    CliArgsError,
    CliHelp,
    CliOptions,
    parse_cli_args,
)
from my_agents.presentation.cli.event_notifier import CliEventNotifier
from my_agents.presentation.cli.repl import CliRepl, write_stderr
from my_agents.presentation.webhook.background_scheduler import (
    BackgroundEventScheduler,
)
from my_agents.presentation.webhook.event_server import EventWebhookServer
from my_agents.usecases.accept_event import AcceptEventHandler
from my_agents.usecases.clear_chat import ClearChatHandler
from my_agents.usecases.finalize_chat import FinalizeChatHandler
from my_agents.usecases.get_chat_history import GetChatHistoryHandler
from my_agents.usecases.get_core_profile import GetCoreProfileHandler
from my_agents.usecases.handle_event import HandleEventHandler
from my_agents.usecases.recall_chat import RecallChatHandler
from my_agents.usecases.send_message import SendMessageHandler
from my_agents.usecases.summarize_episodes import SummarizeEpisodesHandler
from my_agents.usecases.update_core_profile import UpdateCoreProfileHandler


def _build_event_webhook(
    settings: AppSettings,
    chat_repository: SqliteChatRepository,
    *,
    write_output: Callable[[str], None],
    logger: IAppLogger,
) -> IEventWebhookServer | None:
    if settings.event_webhook_port is None:
        return None
    notifier = CliEventNotifier(write_output, logger=logger)
    recorder = SqliteEventChatRecorder(chat_repository)
    scheduler = BackgroundEventScheduler(
        HandleEventHandler(recorder, notifier),
        delay_ms=settings.event_analysis_delay_ms,
        logger=logger,
    )
    accept_event = AcceptEventHandler(scheduler)
    return EventWebhookServer(
        accept_event,
        scheduler,
        port=settings.event_webhook_port,
        logger=logger,
    )


def build_cli(
    *,
    env: Mapping[str, str] | None = None,
    env_path: str | Path | None = None,
    read_input: Callable[[str], str] = input,
    write_output: Callable[[str], None] = print,
    write_error: Callable[[str], None] = write_stderr,
    debug: bool = False,
    logger: IAppLogger = NOOP_LOGGER,
    episode_summarizer: IEpisodeSummarizer | None = None,
) -> Result[CliRepl, SettingsError]:
    resolved_env_path = Path(env_path) if env_path is not None else Path.cwd() / ".env"

    settings_result = load_app_settings(env=env, env_path=resolved_env_path)
    if is_err(settings_result):
        return Err(settings_result.error)

    settings = settings_result.value
    store = MarkdownCoreProfileStore.from_memory_dir(settings.memory_dir)
    get_core_profile = GetCoreProfileHandler(store)
    update_core_profile = UpdateCoreProfileHandler(store)
    chat_repository = SqliteChatRepository.from_settings(settings)
    chat_recall = RecallChatHandler(chat_repository)
    if episode_summarizer is not None:
        summarizer: IEpisodeSummarizer = episode_summarizer
    elif settings.chat_backend == "local":
        summarizer = LocalEpisodeSummarizer()
    else:
        summarizer = PydanticAIEpisodeSummarizer.from_settings(settings)
    if settings.chat_backend == "local":
        agent_runner: IAgentRunner = LocalAgentRunner()
    else:
        agent_runner = PydanticAIAgentRunner.from_settings(
            settings,
            core_profile_updater=update_core_profile,
            chat_recall=chat_recall,
        )
    send_message = SendMessageHandler(
        agent_runner,
        get_core_profile,
        chat_store=chat_repository,
        chat_query=chat_repository,
        episode_summarizer=summarizer,
        summary_index_char_budget=settings.summary_index_char_budget,
        summary_every_messages=settings.summary_every_messages,
        recent_messages_limit=settings.recent_messages_limit,
        episode_max_messages=settings.episode_max_messages,
        episode_gap_minutes=settings.episode_gap_minutes,
    )
    return Ok(
        CliRepl(
            send_message,
            get_core_profile,
            get_chat_history=GetChatHistoryHandler(chat_repository),
            clear_chat=ClearChatHandler(chat_repository, chat_repository, summarizer),
            finalize_chat=FinalizeChatHandler(
                chat_repository, chat_repository, summarizer
            ),
            summarize_episodes=SummarizeEpisodesHandler(
                chat_repository, chat_repository, summarizer
            ),
            read_input=read_input,
            write_output=write_output,
            write_error=write_error,
            debug=debug,
            logger=logger,
            event_webhook=_build_event_webhook(
                settings,
                chat_repository,
                write_output=write_output,
                logger=logger,
            ),
        )
    )


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    env_path: str | Path | None = None,
    read_input: Callable[[str], str] = input,
    write_output: Callable[[str], None] = print,
    write_error: Callable[[str], None] = write_stderr,
    logger: IAppLogger | None = None,
    episode_summarizer: IEpisodeSummarizer | None = None,
) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    parsed = parse_cli_args(raw_argv)
    match parsed:
        case CliHelp(text=help_text):
            write_output(help_text)
            return 0
        case CliArgsError(message=error_message):
            write_error(error_message)
            return 2
        case CliOptions() as options:
            pass
        case _:
            assert_never(parsed)

    with ExitStack() as stack:
        active_log_path: Path | None = None
        if logger is not None:
            active_logger = logger
        else:
            resolution = resolve_log_path(cli_log_file=options.log_file, env=env)
            open_result = JsonlFileLogger.open(
                resolution.path,
                on_write_error=write_error,
            )
            if is_err(open_result):
                if resolution.explicit:
                    write_error(f"Error: {open_result.error.message}")
                    return 1
                write_error(f"Warning: {open_result.error.message}")
                active_logger = NOOP_LOGGER
            else:
                active_logger = stack.enter_context(open_result.value)
                active_log_path = resolution.path

        if options.debug and active_log_path is not None:
            write_error(f"Debug: log file = {active_log_path}")

        result = build_cli(
            env=env,
            env_path=env_path,
            read_input=read_input,
            write_output=write_output,
            write_error=write_error,
            debug=options.debug,
            logger=active_logger,
            episode_summarizer=episode_summarizer,
        )
        if is_err(result):
            active_logger.log(
                LogEntry(
                    level=LogLevel.ERROR,
                    event="settings_load_failed",
                    message=result.error.message,
                )
            )
            write_error(f"Error: {result.error.message}")
            return 1

        result.value.run()
        return 0
