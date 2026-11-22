import json
from collections.abc import Callable
from pathlib import Path

import pytest
from flow_res import Ok, Result, is_ok
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from my_agents.composition import build_cli, run_cli
from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.association_expander import (
    AssociationExpandError,
    AssociationExpandOutcome,
    AssociationExpandRequest,
    IAssociationExpander,
)
from my_agents.contracts.ports.chat_recall import IChatRecall
from my_agents.contracts.ports.core_profile_updater import ICoreProfileUpdater
from my_agents.contracts.ports.episode_summarizer import (
    EpisodeSummarizerError,
    EpisodeSummaryOutcome,
    EpisodeSummaryRequest,
    IEpisodeSummarizer,
)
from my_agents.contracts.ports.logger import NOOP_LOGGER
from my_agents.contracts.secret import SecretStr
from my_agents.domain.context_assembler import assemble_instructions
from my_agents.domain.core_profile import CoreProfile
from my_agents.infrastructure.pydantic_ai_runner import PydanticAIAgentRunner
from my_agents.infrastructure.settings import AppSettings, SettingsError
from tests.helpers import assert_err

_VALID_ENV = {
    "MAGPIE_MODEL": "test-model",
    "MAGPIE_BASE_URL": "https://example.com/v1",
    "MAGPIE_API_KEY": "secret",
}


class StubSummarizer(IEpisodeSummarizer):
    def __init__(self) -> None:
        self.requests: list[EpisodeSummaryRequest] = []

    @classmethod
    def from_settings(cls, _settings: AppSettings) -> IEpisodeSummarizer:
        return cls()

    async def summarize(
        self, request: EpisodeSummaryRequest
    ) -> Result[EpisodeSummaryOutcome, EpisodeSummarizerError]:
        self.requests.append(request)
        return Ok(EpisodeSummaryOutcome("話題", "概要", "要約"))


def _patch_runner(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
    reply: str,
) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions
        captured["messages"] = messages
        last_message = messages[-1]
        assert isinstance(last_message, ModelRequest)
        last_part = last_message.parts[0]
        assert isinstance(last_part, UserPromptPart)
        captured["prompt"] = last_part.content
        return ModelResponse([TextPart(reply)])

    class StubRunner:
        @classmethod
        def from_settings(
            cls,
            settings: AppSettings,
            *,
            core_profile_updater: ICoreProfileUpdater | None = None,
            association_expander: IAssociationExpander | None = None,
            chat_recall: IChatRecall | None = None,
        ) -> PydanticAIAgentRunner:
            captured["model_name"] = settings.model_name
            captured["base_url"] = settings.base_url
            captured["api_key"] = settings.api_key
            captured["memory_dir"] = settings.memory_dir
            captured["updater"] = core_profile_updater
            captured["association_expander"] = association_expander
            captured["chat_recall"] = chat_recall
            return PydanticAIAgentRunner(
                FunctionModel(respond),
                core_profile_updater=core_profile_updater,
                association_expander=association_expander,
                chat_recall=chat_recall,
            )

    monkeypatch.setattr("my_agents.composition.PydanticAIAgentRunner", StubRunner)
    monkeypatch.setattr(
        "my_agents.composition.PydanticAIEpisodeSummarizer", StubSummarizer
    )


def _patch_core_memory_runner(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
    instructions_log: list[str | None],
) -> None:
    tool_calls_remaining = [1]

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        instructions_log.append(info.instructions)
        if tool_calls_remaining[0] > 0:
            tool_calls_remaining[0] -= 1
            return ModelResponse(
                [
                    ToolCallPart(
                        "update_core_memory",
                        {
                            "action": "append",
                            "reason": "新しい価値観",
                            "content": "## 価値観・判断軸\n- 誠実さ",
                        },
                    )
                ]
            )
        return ModelResponse([TextPart("了解しました")])

    class StubRunner:
        @classmethod
        def from_settings(
            cls,
            settings: AppSettings,
            *,
            core_profile_updater: ICoreProfileUpdater | None = None,
            association_expander: IAssociationExpander | None = None,
            chat_recall: IChatRecall | None = None,
        ) -> PydanticAIAgentRunner:
            captured["memory_dir"] = settings.memory_dir
            captured["updater"] = core_profile_updater
            captured["association_expander"] = association_expander
            captured["chat_recall"] = chat_recall
            return PydanticAIAgentRunner(
                FunctionModel(respond),
                core_profile_updater=core_profile_updater,
                association_expander=association_expander,
                chat_recall=chat_recall,
            )

    monkeypatch.setattr("my_agents.composition.PydanticAIAgentRunner", StubRunner)
    monkeypatch.setattr(
        "my_agents.composition.PydanticAIEpisodeSummarizer", StubSummarizer
    )


def _read_inputs(inputs: list[str]) -> Callable[[str], str]:
    def read_input(_prompt: str) -> str:
        return inputs.pop(0)

    return read_input


def test_build_cli_reports_settings_error(tmp_path: Path) -> None:
    result = build_cli(
        env={"MAGPIE_MODEL": "test-model"},
        env_path=tmp_path / ".env",
    )

    error = assert_err(result, message_contains="MAGPIE_API_KEY")
    assert isinstance(error, SettingsError)
    assert "MAGPIE_BASE_URL" in error.message


def test_build_cli_wires_runner_into_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "配線済み")
    outputs: list[str] = []
    env = {**_VALID_ENV, "MEMORY_PATH": str(tmp_path / "agent-memory")}

    result = build_cli(
        env=env,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["こんにちは", "exit"]),
        write_output=outputs.append,
    )

    assert is_ok(result)
    result.unwrap().run()
    assert captured["model_name"] == "test-model"
    assert captured["base_url"] == "https://example.com/v1"
    assert captured["api_key"] == SecretStr("secret")
    assert captured["memory_dir"] == tmp_path / "agent-memory"
    assert captured["updater"] is not None
    assert captured["association_expander"] is None
    assert captured["instructions"] == assemble_instructions(
        CoreProfile.empty(), recall_tools_enabled=True
    )
    assert captured["prompt"] == "こんにちは"
    assert "\nAI > 配線済み" in outputs


def test_build_cli_persists_core_memory_and_uses_it_on_next_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    instructions_log: list[str | None] = []
    _patch_core_memory_runner(monkeypatch, captured, instructions_log)
    outputs: list[str] = []
    memory_dir = tmp_path / "memory"
    env = {**_VALID_ENV, "MEMORY_PATH": str(memory_dir)}

    result = build_cli(
        env=env,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["価値観を覚えて", "ありがとう", "/core", "exit"]),
        write_output=outputs.append,
    )

    assert is_ok(result)
    result.unwrap().run()

    assert (memory_dir / "core.md").read_text(encoding="utf-8") == (
        "## 価値観・判断軸\n- 誠実さ\n"
    )
    assert captured["memory_dir"] == memory_dir
    assert len(instructions_log) == 3
    assert instructions_log[0] is not None
    assert "まだ何も記録されていません。" in instructions_log[0]
    assert instructions_log[2] is not None
    assert "文字数: 16 / 4096" in instructions_log[2]
    assert "## 価値観・判断軸\n- 誠実さ" in instructions_log[2]
    assert outputs[0] == "会話を開始します。コマンド: /help  終了: exit / quit"
    assert outputs[1] == "\n[Core Memory updated: 新しい価値観]"
    assert outputs[2] == "\nAI > 了解しました"
    assert outputs[3] == "\nAI > 了解しました"
    assert outputs[4] == (
        f"\nCore Memory: {memory_dir / 'core.md'}"
        "\n使用量: 16 / 4096"
        "\n## 価値観・判断軸\n- 誠実さ"
    )
    assert outputs[5] == "\n会話を終了しました。"


def test_run_cli_returns_zero_and_uses_default_env_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "MAGPIE_MODEL=file-model\n"
        "MAGPIE_BASE_URL=https://file.example.com/v1\n"
        "MAGPIE_API_KEY=file-secret\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "応答")
    outputs: list[str] = []

    exit_code = run_cli(
        argv=(),
        env={},
        read_input=_read_inputs(["質問", "exit"]),
        write_output=outputs.append,
    )

    assert exit_code == 0
    assert captured["model_name"] == "file-model"
    assert captured["base_url"] == "https://file.example.com/v1"
    assert captured["api_key"] == SecretStr("file-secret")
    assert captured["memory_dir"] == tmp_path / "memory"
    assert captured["instructions"] == assemble_instructions(
        CoreProfile.empty(), recall_tools_enabled=True
    )
    assert captured["prompt"] == "質問"
    assert "\nAI > 応答" in outputs


def test_run_cli_reports_settings_error_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = run_cli(argv=(), env={}, env_path=tmp_path / ".env")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("Error: ")
    assert "MAGPIE_MODEL" in captured.err


def test_run_cli_uses_injected_error_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    errors: list[str] = []

    exit_code = run_cli(
        argv=(),
        env={"MAGPIE_MODEL": "test-model"},
        env_path=tmp_path / ".env",
        write_error=errors.append,
    )

    assert exit_code == 1
    assert len(errors) == 1
    assert errors[0].startswith("Error: ")
    assert "MAGPIE_BASE_URL" in errors[0]


def test_run_cli_reads_sys_argv_when_argv_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "my_agents.composition.sys.argv",
        ["my-agents", "--help"],
    )
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        env={},
        env_path=tmp_path / ".env",
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 0
    assert errors == []
    assert len(outputs) == 1
    assert "--debug" in outputs[0]
    assert "--log-file" in outputs[0]


def test_run_cli_returns_exit_2_on_invalid_arguments(tmp_path: Path) -> None:
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--invalid-option",),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 2
    assert outputs == []
    assert len(errors) == 1
    assert "--invalid-option" in errors[0]


def test_run_cli_debug_and_explicit_log_file_record_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def failing_respond(
        _messages: list[ModelMessage],
        _info: AgentInfo,
    ) -> ModelResponse:
        raise ModelAPIError("test", "upstream 502 error")

    class FailingStubRunner:
        @classmethod
        def from_settings(
            cls,
            settings: AppSettings,
            *,
            core_profile_updater: ICoreProfileUpdater | None = None,
            association_expander: IAssociationExpander | None = None,
            chat_recall: IChatRecall | None = None,
        ) -> PydanticAIAgentRunner:
            return PydanticAIAgentRunner(
                FunctionModel(failing_respond),
                api_key=settings.api_key,
                core_profile_updater=core_profile_updater,
                association_expander=association_expander,
                chat_recall=chat_recall,
            )

    monkeypatch.setattr(
        "my_agents.composition.PydanticAIAgentRunner", FailingStubRunner
    )
    log_path = tmp_path / "logs" / "session.jsonl"
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--debug", "--log-file", str(log_path)),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["質問", "exit"]),
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 0
    assert errors[0] == f"Debug: log file = {log_path}"
    assert errors[1].startswith("\nError: ")
    assert "Debug [API_ERROR]: ModelAPIError: upstream 502 error" in errors[2]

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["level"] == "ERROR"
    assert records[0]["event"] == "send_message_failed"
    assert records[0]["error_type"] == "API_ERROR"
    assert "upstream 502 error" in records[0]["message"]


def test_run_cli_debug_with_injected_logger_skips_log_path_banner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "ok")
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--debug",),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["hi", "exit"]),
        write_output=outputs.append,
        write_error=errors.append,
        logger=NOOP_LOGGER,
    )

    assert exit_code == 0
    assert errors == []
    assert "\nAI > ok" in outputs


def test_run_cli_logs_settings_error_to_log_file(tmp_path: Path) -> None:
    log_path = tmp_path / "settings.jsonl"
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--log-file", str(log_path)),
        env={"MAGPIE_MODEL": "test-model"},
        env_path=tmp_path / ".env",
        write_error=errors.append,
    )

    assert exit_code == 1
    assert errors[0].startswith("Error: ")
    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["event"] == "settings_load_failed"
    assert "MAGPIE_API_KEY" in records[0]["message"]


def test_run_cli_fails_when_explicit_log_file_cannot_be_opened(
    tmp_path: Path,
) -> None:
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--log-file", str(tmp_path)),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        write_error=errors.append,
    )

    assert exit_code == 1
    assert len(errors) == 1
    assert errors[0].startswith("Error: Failed to open log file")


def test_run_cli_warns_and_continues_when_default_log_path_unwritable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "続行できた")
    blocked_state_home = tmp_path / "blocked-state-home"
    blocked_state_home.write_text("not a directory", encoding="utf-8")
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=(),
        env={
            **_VALID_ENV,
            "XDG_STATE_HOME": str(blocked_state_home),
        },
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["質問", "exit"]),
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 0
    assert len(errors) == 1
    assert errors[0].startswith("Warning: Failed to open log file")
    assert "\nAI > 続行できた" in outputs


def test_build_cli_wires_mlx_lora_association_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    sentinel_generator = object()
    sentinel_expander = object()
    adapter_dir = tmp_path / "adapters"

    class StubMlxLoraGenerator:
        @staticmethod
        def is_available(adapter_path: Path) -> bool:
            captured["checked_adapter_path"] = adapter_path
            return True

        @classmethod
        def from_settings(
            cls,
            model_name: str,
            adapter_path: Path,
        ) -> object:
            captured["mlx_lora_model"] = model_name
            captured["mlx_lora_adapter_path"] = adapter_path
            return sentinel_generator

    def fake_expander_factory(generator: object) -> object:
        captured["generator"] = generator
        return sentinel_expander

    _patch_runner(monkeypatch, captured, "配線済み")
    monkeypatch.setattr(
        "my_agents.composition.MlxLoraAssociationGenerator",
        StubMlxLoraGenerator,
    )
    monkeypatch.setattr(
        "my_agents.composition.ExpandAssociationsHandler",
        fake_expander_factory,
    )
    env = {
        **_VALID_ENV,
        "MLX_LORA_MODEL": "custom-mlx-model",
        "MLX_LORA_ADAPTER_PATH": str(adapter_dir),
        "MEMORY_PATH": str(tmp_path / "memory"),
    }

    result = build_cli(env=env, env_path=tmp_path / ".env")

    assert is_ok(result)
    assert captured["checked_adapter_path"] == adapter_dir
    assert captured["mlx_lora_model"] == "custom-mlx-model"
    assert captured["mlx_lora_adapter_path"] == adapter_dir
    assert captured["generator"] is sentinel_generator
    assert captured["association_expander"] is sentinel_expander


def test_build_cli_skips_mlx_lora_association_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    adapter_dir = tmp_path / "missing-adapters"

    class UnavailableMlxLoraGenerator:
        @staticmethod
        def is_available(adapter_path: Path) -> bool:
            captured["checked_adapter_path"] = adapter_path
            return False

    _patch_runner(monkeypatch, captured, "フォールバック")
    monkeypatch.setattr(
        "my_agents.composition.MlxLoraAssociationGenerator",
        UnavailableMlxLoraGenerator,
    )
    env = {
        **_VALID_ENV,
        "MLX_LORA_ADAPTER_PATH": str(adapter_dir),
        "MEMORY_PATH": str(tmp_path / "memory"),
    }

    result = build_cli(env=env, env_path=tmp_path / ".env")

    assert is_ok(result)
    assert captured["checked_adapter_path"] == adapter_dir
    assert captured["association_expander"] is None


def test_build_and_run_cli_accept_injected_association_expander(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class InjectedExpander(IAssociationExpander):
        async def expand(
            self,
            request: AssociationExpandRequest,
        ) -> Result[AssociationExpandOutcome, AssociationExpandError]:
            return Ok(
                AssociationExpandOutcome(
                    root=request.topic,
                    tree_summary=f"root: {request.topic}",
                    lateral_ideas=(),
                )
            )

    class FailIfCalledMlxGenerator:
        @staticmethod
        def is_available(_adapter_path: Path) -> bool:
            raise AssertionError("default MLX wiring should not run when injected")

    captured: dict[str, object] = {}
    injected = InjectedExpander()
    _patch_runner(monkeypatch, captured, "注入済み")
    monkeypatch.setattr(
        "my_agents.composition.MlxLoraAssociationGenerator",
        FailIfCalledMlxGenerator,
    )
    outputs: list[str] = []
    env = {
        **_VALID_ENV,
        "MLX_LORA_ADAPTER_PATH": str(tmp_path / "adapters"),
        "MEMORY_PATH": str(tmp_path / "memory"),
    }

    exit_code = run_cli(
        argv=(),
        env=env,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["こんにちは", "exit"]),
        write_output=outputs.append,
        association_expander=injected,
    )

    assert exit_code == 0
    assert captured["association_expander"] is injected
    assert "\nAI > 注入済み" in outputs


def test_cli_persists_chat_displays_history_resumes_and_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "stored answer")
    summarizer = StubSummarizer()
    db_path = tmp_path / "chat-data" / "conversation.sqlite"
    outputs: list[str] = []
    errors: list[str] = []
    env = {
        **_VALID_ENV,
        "MEMORY_PATH": str(tmp_path / "memory"),
        "CHATS_DB_PATH": str(db_path),
    }

    def run(inputs: list[str]) -> None:
        assert (
            run_cli(
                [],
                env=env,
                env_path=tmp_path / ".env",
                read_input=_read_inputs(inputs),
                write_output=outputs.append,
                write_error=errors.append,
                logger=NOOP_LOGGER,
                episode_summarizer=summarizer,
            )
            == 0
        )

    run(["first question", "/history", "/history 1", "exit"])
    assert db_path.is_file()
    assert "(進行中)" in "\n".join(outputs)
    assert "You > first question" in outputs
    assert "AI > stored answer" in outputs
    assert summarizer.requests == [
        EpisodeSummaryRequest(
            "first question",
            "",
            (
                ChatMessage("user", "first question"),
                ChatMessage("assistant", "stored answer"),
            ),
        )
    ]
    outputs.clear()
    run(["second question", "/history", "/history 1", "/clear", "exit"])
    assert "## 直近の話題のサマリー" in str(captured["instructions"])
    assert "first question" in str(captured["messages"])
    assert "  概要" in outputs
    assert "\nサマリー\n要約\n" in outputs
    assert "You > first question" in outputs
    assert "You > second question" in outputs
    assert (summarizer.requests[1].previous_summary, summarizer.requests[1].turns) == (
        "要約",
        (
            ChatMessage("user", "second question"),
            ChatMessage("assistant", "stored answer"),
        ),
    )
    outputs.clear()
    run(["fresh question", "/history", "/history 2", "exit"])
    assert "## 過去の会話サマリー一覧" in str(captured["instructions"])
    assert "## 直近の話題のサマリー" not in str(captured["instructions"])
    assert "first question" not in str(captured["messages"])
    assert "You > first question" in outputs
    assert "You > second question" in outputs
    assert errors == []
    recall = captured["chat_recall"]
    assert isinstance(recall, IChatRecall)
    [old] = recall.search_episodes("first question").unwrap()
    assert old.turn_count == 4
    assert (
        recall.recall_episode(old.episode_id).unwrap().turns[-1].content
        == "stored answer"
    )


def test_history_detail_without_summary_and_closed_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_runner(monkeypatch, {}, "reply")
    outputs: list[str] = []
    env = {**_VALID_ENV, "MEMORY_PATH": str(tmp_path)}
    cli = build_cli(
        env=env,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["first", "/history 1", "/clear", "/history", "exit"]),
        write_output=outputs.append,
    ).unwrap()
    cli.run()
    assert "You > first" in outputs
    assert not any("サマリー\n" in output for output in outputs)
    assert any(
        output.startswith("1.") and output.endswith("話題 (2 件)") for output in outputs
    )
