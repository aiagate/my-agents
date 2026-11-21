from collections.abc import Callable, Sequence

import pytest
from flow_res import Err, Ok, Result

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    IAgentRunner,
)
from my_agents.presentation.cli import CliRepl, SendMessageUseCase
from my_agents.usecases.send_message import (
    DEFAULT_INSTRUCTIONS,
    SendMessageError,
    SendMessageErrorType,
    SendMessageHandler,
    SendMessageRequest,
    SendMessageResponse,
)

_START_BANNER = "会話を開始します。終了: exit / quit"
_END_BANNER = "\n会話を終了しました。"


class FakeSendMessageUseCase:
    def __init__(
        self,
        results: Sequence[Result[SendMessageResponse, SendMessageError]],
    ) -> None:
        self._results = results
        self.requests: list[SendMessageRequest] = []

    async def execute(
        self,
        request: SendMessageRequest,
    ) -> Result[SendMessageResponse, SendMessageError]:
        self.requests.append(request)
        return self._results[len(self.requests) - 1]


class RaisingSendMessageUseCase:
    def __init__(self, exc_type: type[BaseException]) -> None:
        self._exc_type = exc_type
        self.requests: list[SendMessageRequest] = []

    async def execute(
        self,
        request: SendMessageRequest,
    ) -> Result[SendMessageResponse, SendMessageError]:
        self.requests.append(request)
        raise self._exc_type


class FakeAgentRunner(IAgentRunner):
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.runs: list[tuple[str, tuple[ChatMessage, ...], str]] = []

    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]:
        self.runs.append((user_input, tuple(history), instructions))
        return Ok(AgentReply(content=self._reply))


def _read_inputs(inputs: list[str]) -> Callable[[str], str]:
    def read_input(_prompt: str) -> str:
        return inputs.pop(0)

    return read_input


def _run_repl(
    use_case: SendMessageUseCase,
    inputs: list[str],
) -> tuple[list[str], list[str], list[str]]:
    prompts: list[str] = []
    outputs: list[str] = []
    errors: list[str] = []

    def read_input(prompt: str) -> str:
        prompts.append(prompt)
        return inputs.pop(0)

    CliRepl(
        use_case,
        read_input=read_input,
        write_output=outputs.append,
        write_error=errors.append,
    ).run()
    return prompts, outputs, errors


def test_multi_turn_conversation_propagates_history() -> None:
    first_history = (
        ChatMessage(role="user", content="こんにちは"),
        ChatMessage(role="assistant", content="やあ"),
    )
    second_history = (
        *first_history,
        ChatMessage(role="user", content="元気？"),
        ChatMessage(role="assistant", content="元気だよ"),
    )
    fake = FakeSendMessageUseCase(
        [
            Ok(SendMessageResponse(reply="やあ", history=first_history)),
            Ok(SendMessageResponse(reply="元気だよ", history=second_history)),
        ]
    )
    use_case: SendMessageUseCase = fake

    prompts, outputs, errors = _run_repl(use_case, ["こんにちは", "元気？", "exit"])

    assert prompts == ["\nYou > ", "\nYou > ", "\nYou > "]
    assert outputs == [
        _START_BANNER,
        "\nAI > やあ",
        "\nAI > 元気だよ",
        _END_BANNER,
    ]
    assert errors == []
    assert fake.requests[0] == SendMessageRequest(
        user_input="こんにちは",
        history=(),
    )
    assert fake.requests[1] == SendMessageRequest(
        user_input="元気？",
        history=first_history,
    )


@pytest.mark.parametrize("command", ["exit", "quit", "EXIT", "  Quit  "])
def test_exit_commands_end_session(command: str) -> None:
    fake = FakeSendMessageUseCase([])

    prompts, outputs, errors = _run_repl(fake, [command])

    assert prompts == ["\nYou > "]
    assert outputs == [_START_BANNER, _END_BANNER]
    assert errors == []
    assert fake.requests == []


def test_empty_input_is_skipped() -> None:
    fake = FakeSendMessageUseCase([])

    prompts, outputs, errors = _run_repl(fake, ["", "   ", "exit"])

    assert prompts == ["\nYou > ", "\nYou > ", "\nYou > "]
    assert outputs == [_START_BANNER, _END_BANNER]
    assert errors == []
    assert fake.requests == []


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, EOFError])
def test_input_interruptions_end_session(exc_type: type[BaseException]) -> None:
    fake = FakeSendMessageUseCase([])
    outputs: list[str] = []
    errors: list[str] = []

    def read_input(_prompt: str) -> str:
        raise exc_type

    CliRepl(
        fake,
        read_input=read_input,
        write_output=outputs.append,
        write_error=errors.append,
    ).run()

    assert outputs == [_START_BANNER, _END_BANNER]
    assert errors == []
    assert fake.requests == []


def test_error_result_keeps_history_and_continues() -> None:
    error = SendMessageError(
        type=SendMessageErrorType.UNEXPECTED,
        message="internal detail",
        display_message="一時的なエラーです。",
    )
    first_history = (
        ChatMessage(role="user", content="こんにちは"),
        ChatMessage(role="assistant", content="やあ"),
    )
    third_history = (
        *first_history,
        ChatMessage(role="user", content="次の入力"),
        ChatMessage(role="assistant", content="回復しました"),
    )
    fake = FakeSendMessageUseCase(
        [
            Ok(SendMessageResponse(reply="やあ", history=first_history)),
            Err(error),
            Ok(SendMessageResponse(reply="回復しました", history=third_history)),
        ]
    )

    prompts, outputs, errors = _run_repl(
        fake,
        ["こんにちは", "失敗する入力", "次の入力", "exit"],
    )

    assert prompts == ["\nYou > ", "\nYou > ", "\nYou > ", "\nYou > "]
    assert outputs == [
        _START_BANNER,
        "\nAI > やあ",
        "\nAI > 回復しました",
        _END_BANNER,
    ]
    assert errors == ["\nError: 一時的なエラーです。"]
    assert fake.requests[0].history == ()
    assert fake.requests[1].history == first_history
    assert fake.requests[2].history == first_history


def test_keyboard_interrupt_during_execute_exits_cleanly() -> None:
    fake = RaisingSendMessageUseCase(KeyboardInterrupt)

    prompts, outputs, errors = _run_repl(fake, ["質問"])

    assert prompts == ["\nYou > "]
    assert outputs == [_START_BANNER, _END_BANNER]
    assert errors == []
    assert len(fake.requests) == 1


def test_eof_error_during_execute_propagates() -> None:
    fake = RaisingSendMessageUseCase(EOFError)
    outputs: list[str] = []
    errors: list[str] = []

    with pytest.raises(EOFError):
        CliRepl(
            fake,
            read_input=_read_inputs(["質問"]),
            write_output=outputs.append,
            write_error=errors.append,
        ).run()

    assert outputs == [_START_BANNER]
    assert errors == []
    assert len(fake.requests) == 1


def test_default_error_writer_uses_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = SendMessageError(
        type=SendMessageErrorType.UNEXPECTED,
        message="internal detail",
        display_message="一時的なエラーです。",
    )
    fake = FakeSendMessageUseCase([Err(error)])
    outputs: list[str] = []

    CliRepl(
        fake,
        read_input=_read_inputs(["失敗する入力", "exit"]),
        write_output=outputs.append,
    ).run()

    captured = capsys.readouterr()
    assert outputs == [_START_BANNER, _END_BANNER]
    assert captured.out == ""
    assert captured.err == "\nError: 一時的なエラーです。\n"


def test_send_message_handler_satisfies_protocol_end_to_end() -> None:
    runner = FakeAgentRunner("こんにちは")
    use_case: SendMessageUseCase = SendMessageHandler(runner)

    prompts, outputs, errors = _run_repl(use_case, ["  今日は？  ", "exit"])

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [_START_BANNER, "\nAI > こんにちは", _END_BANNER]
    assert errors == []
    assert runner.runs == [("今日は？", (), DEFAULT_INSTRUCTIONS)]
