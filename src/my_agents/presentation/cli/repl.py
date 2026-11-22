import asyncio
import sys
from collections.abc import Callable
from datetime import tzinfo

from flow_res import is_err, is_ok

from my_agents.contracts.messages import ChatMessage, ChatWarning
from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
)
from my_agents.domain.chat_history import format_episode_time_range
from my_agents.usecases.clear_chat import ClearChatError, ClearChatUseCase
from my_agents.usecases.finalize_chat import FinalizeChatUseCase
from my_agents.usecases.get_chat_history import (
    EpisodeSummaryView,
    GetChatHistoryError,
    GetChatHistoryUseCase,
)
from my_agents.usecases.get_core_profile import GetCoreProfileUseCase
from my_agents.usecases.send_message import SendMessageRequest, SendMessageUseCase
from my_agents.usecases.summarize_episodes import SummarizeEpisodesUseCase


def write_stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


class CliRepl:
    def __init__(
        self,
        send_message: SendMessageUseCase,
        get_core_profile: GetCoreProfileUseCase,
        *,
        get_chat_history: GetChatHistoryUseCase | None = None,
        clear_chat: ClearChatUseCase | None = None,
        finalize_chat: FinalizeChatUseCase | None = None,
        summarize_episodes: SummarizeEpisodesUseCase | None = None,
        read_input: Callable[[str], str] = input,
        write_output: Callable[[str], None] = print,
        write_error: Callable[[str], None] = write_stderr,
        debug: bool = False,
        logger: IAppLogger = NOOP_LOGGER,
        display_tz: tzinfo | None = None,
    ) -> None:
        self._send_message = send_message
        self._get_core_profile = get_core_profile
        self._get_chat_history = get_chat_history
        self._clear_chat = clear_chat
        self._finalize_chat = finalize_chat
        self._summarize_episodes = summarize_episodes
        self._read_input = read_input
        self._write_output = write_output
        self._write_error = write_error
        self._debug = debug
        self._logger = logger
        self._display_tz = display_tz

    def run(self) -> None:
        history: tuple[ChatMessage, ...] = ()
        self._write_output("会話を開始します。コマンド: /help  終了: exit / quit")

        with asyncio.Runner() as runner:
            while True:
                try:
                    prompt = self._read_input("\nYou > ").strip()
                except EOFError, KeyboardInterrupt:
                    break

                if prompt.lower() in {"exit", "quit"}:
                    break
                if not prompt:
                    continue

                command, *arguments = prompt.lower().split()
                if command == "/history":
                    self._show_history(arguments)
                    continue

                match prompt.lower():
                    case "/core":
                        self._show_core_profile()
                    case "/clear":
                        try:
                            if not self._close_active_episode(runner):
                                continue
                        except KeyboardInterrupt:
                            break
                        history = ()
                        self._write_output("\n会話履歴をリセットしました。")
                    case "/help":
                        self._show_help()
                    case _:
                        try:
                            result = runner.run(
                                self._send_message.execute(
                                    SendMessageRequest(
                                        user_input=prompt,
                                        history=history,
                                    )
                                )
                            )
                        except KeyboardInterrupt:
                            break

                        if is_ok(result):
                            history = result.value.history
                            for reason in result.value.core_memory_updates:
                                self._write_output(f"\n[Core Memory updated: {reason}]")
                            self._write_output(f"\nAI > {result.value.reply}")
                            self._show_warnings(result.value.warnings)
                            if (
                                self._summarize_episodes is not None
                                and result.value.pending_summary_episode_ids
                            ):
                                try:
                                    summary = runner.run(
                                        self._summarize_episodes.execute(
                                            result.value.pending_summary_episode_ids
                                        )
                                    )
                                except KeyboardInterrupt:
                                    break
                                self._show_warnings(summary.warnings)
                        else:
                            error = result.error
                            self._logger.log(
                                LogEntry(
                                    level=LogLevel.ERROR,
                                    event="send_message_failed",
                                    error_type=error.type.name,
                                    message=error.message,
                                    detail=error.detail,
                                )
                            )
                            self._write_error(f"\nError: {error.display_message}")
                            if self._debug:
                                self._write_error(
                                    f"Debug [{error.type.name}]: {error.message}"
                                )
                                if error.detail is not None:
                                    self._write_error(error.detail.rstrip("\n"))

            if self._finalize_chat is not None:
                try:
                    response = runner.run(self._finalize_chat.execute())
                except KeyboardInterrupt:
                    pass
                else:
                    self._show_warnings(response.warnings)

        self._write_output("\n会話を終了しました。")

    def _show_warnings(self, warnings: tuple[ChatWarning, ...]) -> None:
        for warning in warnings:
            self._logger.log(
                LogEntry(
                    level=LogLevel.WARNING,
                    event=warning.event,
                    error_type=warning.error_type,
                    message=warning.message,
                    detail=warning.detail,
                )
            )
            self._write_error(f"Warning: {warning.display_message}")
            if self._debug:
                self._write_error(f"Debug [{warning.error_type}]: {warning.message}")
                if warning.detail is not None:
                    self._write_error(warning.detail.rstrip("\n"))

    def _show_chat_error(
        self, event: str, error: ClearChatError | GetChatHistoryError
    ) -> None:
        self._logger.log(
            LogEntry(
                level=LogLevel.ERROR,
                event=event,
                error_type=error.type.name,
                message=error.message,
            )
        )
        self._write_error(f"\nError: {error.display_message}")
        if self._debug:
            self._write_error(f"Debug [{error.type.name}]: {error.message}")

    def _close_active_episode(self, runner: asyncio.Runner) -> bool:
        if self._clear_chat is not None:
            result = runner.run(self._clear_chat.execute())
            if is_err(result):
                self._show_warnings(result.error.warnings)
                self._show_chat_error("clear_chat_failed", result.error)
                return False
            else:
                self._show_warnings(result.value.warnings)
        return True

    def _show_history(self, arguments: list[str]) -> None:
        if self._get_chat_history is None:
            self._write_error("\nError: 会話履歴は利用できません。")
            return
        if not arguments:
            result = self._get_chat_history.list_episodes()
            if is_err(result):
                self._show_chat_error("chat_history_list_failed", result.error)
                return
            self._write_output(f"\n会話履歴: {result.value.location}")
            if not result.value.episodes:
                self._write_output("(会話履歴はありません)")
            for episode in result.value.episodes:
                self._write_output(self._episode_heading(episode))
                if episode.overview:
                    self._write_output(f"  {episode.overview}")
            return

        try:
            index = int(arguments[0]) if len(arguments) == 1 else 0
        except ValueError:
            index = 0
        if index <= 0:
            self._write_error(
                "\nError: /history <番号> には正の整数を指定してください。"
            )
            return
        detail = self._get_chat_history.get_episode_detail(index)
        if is_err(detail):
            self._show_chat_error("chat_history_detail_failed", detail.error)
            return
        episode = detail.value.episode
        self._write_output(f"\n{self._episode_heading(episode)}")
        if episode.summary:
            self._write_output(f"\nサマリー\n{episode.summary}\n")
        for turn in detail.value.turns:
            label = "You" if turn.role == "user" else "AI"
            self._write_output(f"{label} > {turn.content}")

    def _episode_heading(self, episode: EpisodeSummaryView) -> str:
        time_range = format_episode_time_range(
            episode.started_at, episode.updated_at, tz=self._display_tz
        )
        status = " (進行中)" if episode.is_active else ""
        return f"{episode.index}. [{time_range}] {episode.title} ({episode.turn_count} 件){status}"

    def _show_core_profile(self) -> None:
        result = self._get_core_profile.execute()
        if is_ok(result):
            profile = result.value.profile
            content = "(未記録)" if profile.is_empty else profile.content
            self._write_output(
                f"\nCore Memory: {result.value.location}"
                f"\n使用量: {profile.char_count} / {result.value.max_chars}"
                f"\n{content}"
            )
        else:
            error = result.error
            self._logger.log(
                LogEntry(
                    level=LogLevel.ERROR,
                    event="get_core_profile_failed",
                    error_type=error.type.name,
                    message=error.message,
                )
            )
            self._write_error(f"\nError: {error.display_message}")
            if self._debug:
                self._write_error(f"Debug [{error.type.name}]: {error.message}")

    def _show_help(self) -> None:
        self._write_output(
            "\n利用可能なコマンド:"
            "\n  /core   Core Memory を表示"
            "\n  /history  過去の会話を一覧表示"
            "\n  /history <番号>  会話の詳細を表示"
            "\n  /clear  会話履歴をリセット"
            "\n  /help   このヘルプを表示"
            "\n  exit / quit  終了"
        )
