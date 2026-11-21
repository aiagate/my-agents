import asyncio
import sys
from collections.abc import Callable

from flow_res import is_ok

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
)
from my_agents.usecases.get_core_profile import GetCoreProfileUseCase
from my_agents.usecases.send_message import SendMessageRequest, SendMessageUseCase


def write_stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


class CliRepl:
    def __init__(
        self,
        send_message: SendMessageUseCase,
        get_core_profile: GetCoreProfileUseCase,
        *,
        read_input: Callable[[str], str] = input,
        write_output: Callable[[str], None] = print,
        write_error: Callable[[str], None] = write_stderr,
        debug: bool = False,
        logger: IAppLogger = NOOP_LOGGER,
    ) -> None:
        self._send_message = send_message
        self._get_core_profile = get_core_profile
        self._read_input = read_input
        self._write_output = write_output
        self._write_error = write_error
        self._debug = debug
        self._logger = logger

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

                match prompt.lower():
                    case "/core":
                        self._show_core_profile()
                    case "/clear":
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

        self._write_output("\n会話を終了しました。")

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
            "\n  /clear  会話履歴をリセット"
            "\n  /help   このヘルプを表示"
            "\n  exit / quit  終了"
        )
