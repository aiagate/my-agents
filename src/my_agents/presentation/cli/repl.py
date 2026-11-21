import asyncio
import sys
from collections.abc import Callable
from typing import Protocol

from flow_res import Result, is_ok

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
)
from my_agents.usecases.send_message import (
    SendMessageError,
    SendMessageRequest,
    SendMessageResponse,
)


def write_stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


class SendMessageUseCase(Protocol):
    async def execute(
        self,
        request: SendMessageRequest,
    ) -> Result[SendMessageResponse, SendMessageError]: ...


class CliRepl:
    def __init__(
        self,
        send_message: SendMessageUseCase,
        *,
        read_input: Callable[[str], str] = input,
        write_output: Callable[[str], None] = print,
        write_error: Callable[[str], None] = write_stderr,
        debug: bool = False,
        logger: IAppLogger = NOOP_LOGGER,
    ) -> None:
        self._send_message = send_message
        self._read_input = read_input
        self._write_output = write_output
        self._write_error = write_error
        self._debug = debug
        self._logger = logger

    def run(self) -> None:
        history: tuple[ChatMessage, ...] = ()
        self._write_output("会話を開始します。終了: exit / quit")

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

                try:
                    result = runner.run(
                        self._send_message.execute(
                            SendMessageRequest(user_input=prompt, history=history)
                        )
                    )
                except KeyboardInterrupt:
                    break

                if is_ok(result):
                    history = result.value.history
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
                        self._write_error(f"Debug [{error.type.name}]: {error.message}")
                        if error.detail is not None:
                            self._write_error(error.detail.rstrip("\n"))

        self._write_output("\n会話を終了しました。")
