from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from flow_res import Err, Result, is_ok
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from my_agents.core.context import AssembledContext
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    DialogueTurn,
    EventDelivery,
    EventEnvelope,
    EventRecord,
    EventStatus,
    MemoryBlock,
    MemoryBlocks,
    MemoryStatus,
    TurnOrigin,
)
from my_agents.shell.llm import (
    ChatToolbox,
    Llm,
    _event_name,
    _last_tool_return,
    _last_user,
    _local_brief,
    _local_external_tool,
)
from my_agents.shell.logger import NOOP_LOGGER
from my_agents.shell.settings import load_settings
from my_agents.shell.store import Store
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def _paths(tmp_path: Path) -> Store:
    return Store(tmp_path / "chats.db", tmp_path / "core.md")


def _llm(store: Store, tmp_path: Path, *, backend: str = "local") -> Llm:
    settings = load_settings(
        env={"MEMORY_PATH": str(tmp_path), "CHAT_BACKEND": backend}
    ).unwrap()
    return Llm.from_settings(settings, store=store, logger=NOOP_LOGGER).unwrap()


def _envelope(name: str, data: dict[str, object]) -> EventEnvelope:
    return EventEnvelope("evt_1", "c", name, "source", _NOW, "key", data)


def _context(event_id: str | None = None) -> AssembledContext:
    section = "" if event_id is None else f"## events\n\n- [event:{event_id}] ambient"
    return AssembledContext("stable", section, f"stable\n{section}".strip(), 0, 0)


@pytest.mark.anyio
async def test_chat_returns_local_reply_and_references_events(tmp_path: Path) -> None:
    store = _paths(tmp_path)
    llm = _llm(store, tmp_path)
    history = (
        DialogueTurn("c", "e", 1, 1, "user", TurnOrigin.USER, "前", None, _NOW),
        DialogueTurn(
            "c", "e", 2, 2, "assistant", TurnOrigin.REPLY, "前回答", None, _NOW
        ),
    )
    normal = await llm.chat("c", "こんにちは", history, _context())
    assert normal.unwrap().text == "ローカル応答: こんにちは"
    referenced = await llm.chat("c", "イベントは？", (), _context("evt_abc"))
    assert "evt_abc" in referenced.unwrap().text
    empty = await llm.chat("c", "event?", (), _context())
    assert "イベントはありません" in empty.unwrap().text


@pytest.mark.anyio
async def test_distill_handles_temperature_email_and_generic_events(
    tmp_path: Path,
) -> None:
    llm = _llm(_paths(tmp_path), tmp_path)
    temperature = await llm.distill(
        _envelope(
            "temperature.threshold_exceeded",
            {"celsius": 33.2, "threshold_celsius": 30.0},
        )
    )
    assert temperature.unwrap().urgency == 8
    urgent = await llm.distill(
        _envelope("email.received", {"sender": "a", "subject": "至急 障害"})
    )
    assert urgent.unwrap().requires_user_action
    routine = await llm.distill(
        _envelope("email.received", {"sender": "a", "subject": "定例"})
    )
    assert not routine.unwrap().requires_user_action
    generic = await llm.distill(_envelope("other.event", {}))
    assert generic.unwrap().dedupe_key == "other.event"


@pytest.mark.anyio
async def test_distill_reports_worker_failure(tmp_path: Path) -> None:
    llm = _llm(_paths(tmp_path), tmp_path)
    error = assert_err(
        await llm.distill(_envelope("spike.worker_failure", {"reason": "boom"})),
        kind=AppErrorKind.LLM,
        message_contains="injected worker failure",
    )
    assert error.kind is AppErrorKind.LLM


@pytest.mark.anyio
async def test_compose_returns_natural_text_and_rejects_leakage(
    tmp_path: Path,
) -> None:
    store = _paths(tmp_path)
    brief = (
        await _llm(store, tmp_path).distill(
            _envelope("temperature.threshold_exceeded", {"celsius": 31})
        )
    ).unwrap()
    natural = await _llm(store, tmp_path).compose(brief, _context())
    assert "室内" in natural.unwrap()


@pytest.mark.anyio
async def test_compose_rejects_machine_output(tmp_path: Path) -> None:
    def leaked(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse([TextPart("[event:evt_1]")])

    llm = Llm(FunctionModel(leaked), store=_paths(tmp_path), logger=NOOP_LOGGER)
    brief = (
        await _llm(_paths(tmp_path), tmp_path).distill(
            _envelope("temperature.threshold_exceeded", {"celsius": 31})
        )
    ).unwrap()
    error = assert_err(
        await llm.compose(brief, _context()),
        kind=AppErrorKind.VALIDATION,
        message_contains="machine-readable",
    )
    assert error.kind is AppErrorKind.VALIDATION


@pytest.mark.anyio
async def test_model_failures_are_wrapped(tmp_path: Path) -> None:
    def broken(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model down")

    llm = Llm(FunctionModel(broken), store=_paths(tmp_path), logger=NOOP_LOGGER)
    assert_err(
        await llm.chat("c", "hi", (), _context()),
        kind=AppErrorKind.LLM,
        message_contains="model down",
    )
    assert_err(
        await llm.distill(_envelope("x", {})),
        kind=AppErrorKind.LLM,
        message_contains="model down",
    )
    brief = (
        await _llm(_paths(tmp_path), tmp_path).distill(
            _envelope("temperature.threshold_exceeded", {"celsius": 31})
        )
    ).unwrap()
    assert_err(
        await llm.compose(brief, _context()),
        kind=AppErrorKind.LLM,
        message_contains="model down",
    )
    assert_err(
        await llm.summarize((), ""),
        kind=AppErrorKind.LLM,
        message_contains="model down",
    )


@pytest.mark.anyio
async def test_summarize_normalizes_output(tmp_path: Path) -> None:
    turns = (DialogueTurn("c", "e", 1, 1, "user", TurnOrigin.USER, "話題", None, _NOW),)
    summary = await _llm(_paths(tmp_path), tmp_path).summarize(turns, "old")
    assert summary.unwrap().title == "話題"
    assert "old" not in summary.unwrap().summary
    assert summary.unwrap().overview == "ローカルモードの決定論的要約"


def test_from_settings_openai_requires_key(tmp_path: Path) -> None:
    store = _paths(tmp_path)
    settings = load_settings(env={"CHAT_BACKEND": "openai"}).unwrap()
    assert_err(
        Llm.from_settings(settings, store=store, logger=NOOP_LOGGER),
        kind=AppErrorKind.VALIDATION,
        message_contains="MAGPIE_API_KEY",
    )
    configured = load_settings(
        env={
            "CHAT_BACKEND": "openai",
            "MAGPIE_MODEL": "m",
            "MAGPIE_BASE_URL": "https://example.test/v1",
            "MAGPIE_API_KEY": "secret",
        }
    ).unwrap()
    assert is_ok(Llm.from_settings(configured, store=store, logger=NOOP_LOGGER))


def _toolbox(store: Store) -> ChatToolbox:
    return ChatToolbox(store, "c", [], _NOW)


def test_toolbox_reads_events_and_recalls_conversations(tmp_path: Path) -> None:
    store = _paths(tmp_path)
    episode, _ = store.record_turns(
        "c",
        None,
        (
            ("user", TurnOrigin.USER, "hello", None),
            ("assistant", TurnOrigin.REPLY, "hi", None),
        ),
        now=_NOW,
        title="topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    store.insert_event(
        EventRecord(
            "evt_1",
            "c",
            "key",
            "name",
            "source",
            _NOW,
            _NOW,
            EventStatus.COMPLETED,
            EventDelivery.AMBIENT,
            "",
            "{}",
            None,
            "",
            None,
            None,
        )
    ).unwrap()
    toolbox = _toolbox(store)
    assert "evt_1" in toolbox.read_event("evt_1")
    assert "not found" in toolbox.read_event("missing")
    assert "topic" in toolbox.search_conversations("topic")
    assert toolbox.search_conversations("nope") == "[]"
    assert "hello" in toolbox.recall_conversation(episode.episode_id)
    assert "not found" in toolbox.recall_conversation("missing")
    assert len(toolbox.toolset().tools) == 5


def test_toolbox_remember_and_update_memory(tmp_path: Path) -> None:
    store = _paths(tmp_path)
    toolbox = _toolbox(store)
    assert "Remembered" in toolbox.remember(MemoryBlock.USER, "likes tea", "reason")
    assert store.list_candidates("c", MemoryStatus.PENDING).unwrap()[0].content == (
        "likes tea"
    )
    assert "updated" in toolbox.update_core_memory("append", MemoryBlock.USER, "hello")
    assert store.read_memory().unwrap().user == "hello"
    assert "rejected" in toolbox.update_core_memory(
        "replace", MemoryBlock.USER, "x", "missing"
    )


def test_toolbox_reports_backing_store_errors(tmp_path: Path) -> None:
    broken_path = tmp_path / "broken.db"
    broken_path.write_text("not a database", encoding="utf-8")
    broken = ChatToolbox(Store(broken_path, tmp_path / "core.md"), "c", [], _NOW)
    assert "Could not read" in broken.read_event("evt")
    assert "Could not search" in broken.search_conversations("q")
    assert "Could not recall" in broken.recall_conversation("e")
    assert "Could not remember" in broken.remember(MemoryBlock.USER, "x", "r")
    memory_dir = tmp_path / "memory-dir"
    memory_dir.mkdir()
    read_error = ChatToolbox(Store(tmp_path / "other.db", memory_dir), "c", [], _NOW)
    assert "Could not read memory" in read_error.update_core_memory(
        "append", MemoryBlock.USER, "x"
    )


class _WriteFailingStore(Store):
    def write_memory(self, blocks: MemoryBlocks) -> Result[None, AppError]:
        del blocks
        return Err(AppError(AppErrorKind.STORAGE, "write failed"))


def test_toolbox_reports_memory_write_failure(tmp_path: Path) -> None:
    store = _WriteFailingStore(tmp_path / "a.db", tmp_path / "core.md")
    toolbox = ChatToolbox(store, "c", [], _NOW)
    assert "Could not save memory" in toolbox.update_core_memory(
        "append", MemoryBlock.USER, "x"
    )


def test_local_helpers_cover_empty_and_malformed_inputs() -> None:
    assert _last_user([]) == ""
    assert _last_user([ModelRequest([])]) == ""
    assert _last_user([ModelRequest([SystemPromptPart("x")])]) == ""
    assert _last_user([ModelResponse([TextPart("x")]), ModelRequest([])]) == ""
    assert _event_name("") == "unknown.event"
    brief = _local_brief("event_name: other.event\n>>\n{bad\n<<END_UNTRUSTED_EVENT")
    assert brief.dedupe_key == "other.event"


def test_recall_reports_turn_read_failure(tmp_path: Path) -> None:
    store = _paths(tmp_path)
    episode, _ = store.record_turns(
        "c",
        None,
        (("user", TurnOrigin.USER, "hello", None),),
        now=_NOW,
        title="topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    with patch.object(
        store,
        "list_turns",
        return_value=Err(AppError(AppErrorKind.STORAGE, "no turns")),
    ):
        assert "Could not recall" in _toolbox(store).recall_conversation(
            episode.episode_id
        )


@pytest.mark.anyio
async def test_external_toolsets_are_accepted(tmp_path: Path) -> None:
    from pydantic_ai import FunctionToolset

    llm = Llm(
        FunctionModel(lambda _messages, _info: ModelResponse([TextPart("ok")])),
        store=_paths(tmp_path),
        logger=NOOP_LOGGER,
        external_toolsets=(FunctionToolset(),),
    )
    reply = await llm.chat("c", "hi", (), _context())
    assert reply.unwrap().text == "ok"


def test_local_external_tool_routing_and_tool_returns() -> None:
    returned = ToolReturnPart("diverge_associations", {"provider": "external-fastmcp"})
    assert _last_tool_return([ModelRequest([returned])]) == returned
    assert _last_tool_return([ModelResponse([TextPart("x")])]) is None
    divergence = _local_external_tool("", {"diverge_associations"}, returned)
    assert isinstance(divergence, ModelResponse)
    divergence_part = divergence.parts[0]
    assert isinstance(divergence_part, TextPart)
    assert "external-fastmcp" in divergence_part.content
    remembered = _local_external_tool(
        "", {"remember"}, ToolReturnPart("remember", "ok")
    )
    assert isinstance(remembered, ModelResponse)
    remembered_part = remembered.parts[0]
    assert isinstance(remembered_part, TextPart)
    assert remembered_part.content == "覚えました。"
    assert _local_external_tool("hello", set(), None) is None
    assert _local_external_tool("hello", set(), ToolReturnPart("other", "x")) is None
    call = _local_external_tool("連想して: 海", {"diverge_associations"}, None)
    assert isinstance(call, ModelResponse)
    part = call.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.args == {"seed": "海", "context": "", "limit": 3}
    bare = _local_external_tool("連想", {"diverge_associations"}, None)
    assert isinstance(bare, ModelResponse)
    bare_part = bare.parts[0]
    assert isinstance(bare_part, ToolCallPart)
    assert bare_part.args == {"seed": "連想", "context": "", "limit": 3}
    remember = _local_external_tool("覚えて: 緑茶", {"remember"}, None)
    assert isinstance(remember, ModelResponse)
    remember_part = remember.parts[0]
    assert isinstance(remember_part, ToolCallPart)
    assert remember_part.args == {
        "block": "user",
        "content": "緑茶",
        "reason": "user asked",
    }
    bare_remember = _local_external_tool("覚えて", {"remember"}, None)
    assert isinstance(bare_remember, ModelResponse)
    bare_remember_part = bare_remember.parts[0]
    assert isinstance(bare_remember_part, ToolCallPart)
    assert bare_remember_part.args == {
        "block": "user",
        "content": "覚えて",
        "reason": "user asked",
    }
    assert (
        _last_user([ModelRequest([ToolReturnPart("x", "y"), UserPromptPart("u")])])
        == "u"
    )
