from collections.abc import Callable, Sequence

import pytest
from flow_res import Err, Ok, Result

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    IAgentRunner,
)
from my_agents.contracts.ports.core_profile_store import (
    CoreProfileStoreError,
    ICoreProfileStore,
)
from my_agents.contracts.ports.logger import IAppLogger, LogEntry, LogLevel
from my_agents.domain.context_assembler import assemble_instructions
from my_agents.domain.core_profile import CoreProfile
from my_agents.presentation.cli import (
    CliRepl,
    GetCoreProfileUseCase,
    SendMessageUseCase,
)
from my_agents.usecases.get_core_profile import (
    GetCoreProfileError,
    GetCoreProfileErrorType,
    GetCoreProfileHandler,
    GetCoreProfileResponse,
)
from my_agents.usecases.send_message import (
    SendMessageError,
    SendMessageErrorType,
    SendMessageHandler,
    SendMessageRequest,
    SendMessageResponse,
)

_START_BANNER = "会話を開始します。コマンド: /help  終了: exit / quit"
_END_BANNER = "\n会話を終了しました。"
_HELP_OUTPUT = (
    "\n利用可能なコマンド:"
    "\n  /core   Core Memory を表示"
    "\n  /clear  会話履歴をリセット"
    "\n  /help   このヘルプを表示"
    "\n  exit / quit  終了"
)


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


class FakeGetCoreProfile:
    def __init__(
        self,
        result: Result[GetCoreProfileResponse, GetCoreProfileError],
    ) -> None:
        self.result = result
        self.calls = 0

    def execute(self) -> Result[GetCoreProfileResponse, GetCoreProfileError]:
        self.calls += 1
        return self.result


class EmptyStore(ICoreProfileStore):
    @property
    def location(self) -> str:
        return "/tmp/memory/core.md"

    def load(self) -> Result[str | None, CoreProfileStoreError]:
        return Ok(None)

    def save(self, content: str) -> Result[None, CoreProfileStoreError]:
        raise AssertionError(f"save must not be called: {content}")


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


class RecordingLogger(IAppLogger):
    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def log(self, entry: LogEntry, /) -> None:
        self.entries.append(entry)


def _profile_response(profile: CoreProfile) -> GetCoreProfileResponse:
    return GetCoreProfileResponse(
        profile=profile,
        location="/tmp/memory/core.md",
        max_chars=4096,
    )


def _empty_get_core_profile() -> FakeGetCoreProfile:
    return FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))


def _read_inputs(inputs: list[str]) -> Callable[[str], str]:
    def read_input(_prompt: str) -> str:
        return inputs.pop(0)

    return read_input


def _run_repl(
    use_case: SendMessageUseCase,
    inputs: list[str],
    get_core_profile: GetCoreProfileUseCase | None = None,
    *,
    debug: bool = False,
    logger: IAppLogger | None = None,
) -> tuple[list[str], list[str], list[str]]:
    prompts: list[str] = []
    outputs: list[str] = []
    errors: list[str] = []

    def read_input(prompt: str) -> str:
        prompts.append(prompt)
        return inputs.pop(0)

    resolved_get_core_profile = (
        _empty_get_core_profile() if get_core_profile is None else get_core_profile
    )
    if logger is None:
        CliRepl(
            use_case,
            resolved_get_core_profile,
            read_input=read_input,
            write_output=outputs.append,
            write_error=errors.append,
            debug=debug,
        ).run()
    else:
        CliRepl(
            use_case,
            resolved_get_core_profile,
            read_input=read_input,
            write_output=outputs.append,
            write_error=errors.append,
            debug=debug,
            logger=logger,
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
        _empty_get_core_profile(),
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

    recorder = RecordingLogger()
    prompts, outputs, errors = _run_repl(
        fake,
        ["こんにちは", "失敗する入力", "次の入力", "exit"],
        logger=recorder,
    )

    assert prompts == ["\nYou > ", "\nYou > ", "\nYou > ", "\nYou > "]
    assert outputs == [
        _START_BANNER,
        "\nAI > やあ",
        "\nAI > 回復しました",
        _END_BANNER,
    ]
    assert errors == ["\nError: 一時的なエラーです。"]
    assert recorder.entries == [
        LogEntry(
            level=LogLevel.ERROR,
            event="send_message_failed",
            error_type="UNEXPECTED",
            message="internal detail",
            detail=None,
        )
    ]
    assert fake.requests[0].history == ()
    assert fake.requests[1].history == first_history
    assert fake.requests[2].history == first_history


def test_debug_mode_writes_error_message_and_optional_detail() -> None:
    error_without_detail = SendMessageError(
        type=SendMessageErrorType.API_ERROR,
        message="ModelAPIError: status 502",
        display_message="APIとの通信でエラーが発生しました。",
    )
    error_with_detail = SendMessageError(
        type=SendMessageErrorType.UNEXPECTED,
        message="RuntimeError: boom",
        display_message="予期しないエラーが発生しました。",
        detail="Traceback (most recent call last):\nRuntimeError: boom\n",
    )
    fake = FakeSendMessageUseCase([Err(error_without_detail), Err(error_with_detail)])
    recorder = RecordingLogger()

    _prompts, outputs, errors = _run_repl(
        fake,
        ["一回目", "二回目", "exit"],
        debug=True,
        logger=recorder,
    )

    assert outputs == [_START_BANNER, _END_BANNER]
    assert errors == [
        "\nError: APIとの通信でエラーが発生しました。",
        "Debug [API_ERROR]: ModelAPIError: status 502",
        "\nError: 予期しないエラーが発生しました。",
        "Debug [UNEXPECTED]: RuntimeError: boom",
        "Traceback (most recent call last):\nRuntimeError: boom",
    ]
    assert recorder.entries == [
        LogEntry(
            level=LogLevel.ERROR,
            event="send_message_failed",
            error_type="API_ERROR",
            message="ModelAPIError: status 502",
            detail=None,
        ),
        LogEntry(
            level=LogLevel.ERROR,
            event="send_message_failed",
            error_type="UNEXPECTED",
            message="RuntimeError: boom",
            detail="Traceback (most recent call last):\nRuntimeError: boom\n",
        ),
    ]


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
            _empty_get_core_profile(),
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
        _empty_get_core_profile(),
        read_input=_read_inputs(["失敗する入力", "exit"]),
        write_output=outputs.append,
    ).run()

    captured = capsys.readouterr()
    assert outputs == [_START_BANNER, _END_BANNER]
    assert captured.out == ""
    assert captured.err == "\nError: 一時的なエラーです。\n"


def test_core_memory_updates_are_rendered_before_reply() -> None:
    history = (
        ChatMessage(role="user", content="覚えて"),
        ChatMessage(role="assistant", content="覚えたよ"),
    )
    fake = FakeSendMessageUseCase(
        [
            Ok(
                SendMessageResponse(
                    reply="覚えたよ",
                    history=history,
                    core_memory_updates=("価値観を記録", "テーマを更新"),
                )
            )
        ]
    )

    prompts, outputs, errors = _run_repl(fake, ["覚えて", "exit"])

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [
        _START_BANNER,
        "\n[Core Memory updated: 価値観を記録]",
        "\n[Core Memory updated: テーマを更新]",
        "\nAI > 覚えたよ",
        _END_BANNER,
    ]
    assert errors == []
    assert fake.requests[0] == SendMessageRequest(user_input="覚えて", history=())


def test_core_command_displays_empty_profile() -> None:
    fake = FakeSendMessageUseCase([])
    get_core_profile = _empty_get_core_profile()

    prompts, outputs, errors = _run_repl(fake, ["/core", "exit"], get_core_profile)

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [
        _START_BANNER,
        "\nCore Memory: /tmp/memory/core.md\n使用量: 0 / 4096\n(未記録)",
        _END_BANNER,
    ]
    assert errors == []
    assert get_core_profile.calls == 1
    assert fake.requests == []


def test_core_command_is_case_insensitive_and_displays_content() -> None:
    profile = CoreProfile(content="## 価値観\n- 誠実さ")
    fake = FakeSendMessageUseCase([])
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(profile)))

    prompts, outputs, errors = _run_repl(fake, ["/CORE", "exit"], get_core_profile)

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [
        _START_BANNER,
        (
            "\nCore Memory: /tmp/memory/core.md"
            f"\n使用量: {profile.char_count} / 4096"
            "\n## 価値観\n- 誠実さ"
        ),
        _END_BANNER,
    ]
    assert errors == []
    assert get_core_profile.calls == 1
    assert fake.requests == []


def test_core_command_reports_error_to_error_writer() -> None:
    fake = FakeSendMessageUseCase([])
    get_core_profile = FakeGetCoreProfile(
        Err(
            GetCoreProfileError(
                type=GetCoreProfileErrorType.INVALID_PROFILE,
                message="too large",
                display_message="Core Memory の内容が不正です。",
            )
        )
    )

    prompts, outputs, errors = _run_repl(fake, ["/core", "exit"], get_core_profile)

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [_START_BANNER, _END_BANNER]
    assert errors == ["\nError: Core Memory の内容が不正です。"]
    assert get_core_profile.calls == 1
    assert fake.requests == []


def test_core_command_failure_logs_and_reports_debug_output() -> None:
    fake = FakeSendMessageUseCase([])
    get_core_profile = FakeGetCoreProfile(
        Err(
            GetCoreProfileError(
                type=GetCoreProfileErrorType.INVALID_PROFILE,
                message="too large",
                display_message="Core Memory の内容が不正です。",
            )
        )
    )
    recorder = RecordingLogger()

    prompts, outputs, errors = _run_repl(
        fake,
        ["/core", "exit"],
        get_core_profile,
        debug=True,
        logger=recorder,
    )

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [_START_BANNER, _END_BANNER]
    assert errors == [
        "\nError: Core Memory の内容が不正です。",
        "Debug [INVALID_PROFILE]: too large",
    ]
    assert recorder.entries == [
        LogEntry(
            level=LogLevel.ERROR,
            event="get_core_profile_failed",
            error_type="INVALID_PROFILE",
            message="too large",
        )
    ]
    assert get_core_profile.calls == 1
    assert fake.requests == []


def test_help_command_lists_available_commands() -> None:
    fake = FakeSendMessageUseCase([])

    prompts, outputs, errors = _run_repl(fake, ["/help", "exit"])

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [_START_BANNER, _HELP_OUTPUT, _END_BANNER]
    assert errors == []
    assert fake.requests == []


def test_clear_command_resets_history() -> None:
    first_history = (
        ChatMessage(role="user", content="こんにちは"),
        ChatMessage(role="assistant", content="やあ"),
    )
    second_history = (
        ChatMessage(role="user", content="別の話題"),
        ChatMessage(role="assistant", content="了解"),
    )
    fake = FakeSendMessageUseCase(
        [
            Ok(SendMessageResponse(reply="やあ", history=first_history)),
            Ok(SendMessageResponse(reply="了解", history=second_history)),
        ]
    )

    prompts, outputs, errors = _run_repl(
        fake,
        ["こんにちは", "/clear", "別の話題", "exit"],
    )

    assert prompts == ["\nYou > ", "\nYou > ", "\nYou > ", "\nYou > "]
    assert outputs == [
        _START_BANNER,
        "\nAI > やあ",
        "\n会話履歴をリセットしました。",
        "\nAI > 了解",
        _END_BANNER,
    ]
    assert errors == []
    assert fake.requests[0].history == ()
    assert fake.requests[1] == SendMessageRequest(user_input="別の話題", history=())


def test_send_message_handler_satisfies_protocol_end_to_end() -> None:
    runner = FakeAgentRunner("こんにちは")
    get_core_profile = GetCoreProfileHandler(EmptyStore())
    use_case: SendMessageUseCase = SendMessageHandler(runner, get_core_profile)

    prompts, outputs, errors = _run_repl(
        use_case,
        ["  今日は？  ", "exit"],
        get_core_profile,
    )

    assert prompts == ["\nYou > ", "\nYou > "]
    assert outputs == [_START_BANNER, "\nAI > こんにちは", _END_BANNER]
    assert errors == []
    assert runner.runs == [
        (
            "今日は？",
            (),
            assemble_instructions(CoreProfile.empty()),
        )
    ]
