import json
import sys
from pathlib import Path
from typing import Self

import pytest
from flow_res import Err, is_ok
from pydantic import SecretStr
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import AbstractToolset

from my_agents.core.types import DEFAULT_PERSONA, AppErrorKind
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import Llm
from my_agents.shell.logger import NOOP_LOGGER
from my_agents.shell.mcp import (
    McpServerSpec,
    build_toolsets,
    parse_mcp_servers,
)
from my_agents.shell.settings import load_settings
from my_agents.shell.store import Store
from tests.helpers import assert_err

_REPO = Path(__file__).resolve().parents[1]
_SERVER = _REPO / "tools" / "mcp_divergence_server.py"


def _settings(tmp_path: Path, mcp: dict[str, object] | None = None):
    env = {
        "MEMORY_PATH": str(tmp_path),
        "CHAT_BACKEND": "local",
        "MCP_SERVERS_JSON": json.dumps(mcp or {}),
    }
    return load_settings(env=env).unwrap()


def test_parse_mcp_servers_builds_stdio_and_http_specs(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "local": {
                "command": sys.executable,
                "args": [str(_SERVER)],
                "env": {"MCP_TEST": "1"},
                "cwd": str(_REPO),
            },
            "remote": {
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer secret"},
            },
        }
    )
    specs = parse_mcp_servers(raw).unwrap()
    assert specs == (
        McpServerSpec(
            name="local",
            command=sys.executable,
            args=(str(_SERVER),),
            env=(("MCP_TEST", SecretStr("1")),),
            cwd=_REPO,
        ),
        McpServerSpec(
            name="remote",
            url="https://example.test/mcp",
            headers=(("Authorization", SecretStr("Bearer secret")),),
        ),
    )
    assert "Bearer secret" not in repr(specs[1])
    toolsets = build_toolsets(specs)
    assert [toolset.id for toolset in toolsets] == ["local", "remote"]
    assert all(toolset.include_instructions is False for toolset in toolsets)
    assert parse_mcp_servers(None).unwrap() == ()
    assert parse_mcp_servers("{}").unwrap() == ()
    assert _settings(tmp_path).mcp_servers.unwrap() == ()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{", "valid JSON"),
        ("[]", "JSON object"),
        (json.dumps({"": {"command": "python"}}), "non-empty strings"),
        (json.dumps({"x": "command"}), "must be a JSON object"),
        (json.dumps({"x": {"command": 1}}), "field 'command'"),
        (json.dumps({"x": {}}), "requires 'command' or 'url'"),
        (json.dumps({"x": {"url": "ftp://example.test"}}), "http:// or https://"),
        (
            json.dumps({"x": {"command": "python", "headers": {"A": "b"}}}),
            "headers require 'url'",
        ),
        (
            json.dumps({"x": {"url": "https://example.test", "command": "python"}}),
            "either 'url' or 'command'",
        ),
    ],
)
def test_parse_mcp_servers_rejects_invalid_shapes(raw: str, message: str) -> None:
    error = assert_err(
        parse_mcp_servers(raw),
        kind=AppErrorKind.VALIDATION,
        message_contains=message,
    )
    assert error.kind is AppErrorKind.VALIDATION


class _FailingToolset(AbstractToolset[None]):
    @property
    def id(self) -> str:
        return "failing"

    async def __aenter__(self) -> Self:
        raise RuntimeError("cannot connect with secret-key")

    async def __aexit__(self, *args: object) -> None:
        del args
        return None

    async def get_tools(self, ctx: object) -> dict:
        del ctx
        return {}

    async def call_tool(self, name: str, tool_args: dict, ctx: object, tool: object):
        del name, tool_args, ctx, tool
        raise LookupError("no tools")


class _CountingToolset(AbstractToolset[None]):
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    @property
    def id(self) -> str:
        return "counting"

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        self.exited += 1
        return None

    async def get_tools(self, ctx: object) -> dict:
        del ctx
        return {}

    async def call_tool(self, name: str, tool_args: dict, ctx: object, tool: object):
        del name, tool_args, ctx, tool
        raise LookupError("no tools")


def _llm(toolsets: tuple[AbstractToolset[None], ...] = ()) -> Llm:
    return Llm(
        FunctionModel(lambda _messages, _info: ModelResponse([TextPart("ok")])),
        store=Store(Path("/tmp/unused.db"), Path("/tmp/unused.md")),
        logger=NOOP_LOGGER,
        external_toolsets=toolsets,
    )


@pytest.mark.anyio
async def test_toolset_lifecycle_is_idempotent() -> None:
    toolset = _CountingToolset()
    llm = _llm((toolset,))
    await llm.start()
    await llm.start()
    await llm.aclose()
    await llm.aclose()
    assert (toolset.entered, toolset.exited) == (1, 1)


@pytest.mark.anyio
async def test_toolset_start_failure_returns_typed_error() -> None:
    llm = Llm(
        FunctionModel(lambda _messages, _info: ModelResponse([TextPart("ok")])),
        store=Store(Path("/tmp/unused.db"), Path("/tmp/unused.md")),
        logger=NOOP_LOGGER,
        external_toolsets=(_FailingToolset(),),
        redactions=("secret-key",),
    )
    error = assert_err(
        await llm.start(),
        kind=AppErrorKind.LLM,
        message_contains="cannot connect",
    )
    assert error.kind is AppErrorKind.LLM
    assert "secret-key" not in error.message
    await llm.aclose()


def test_from_settings_rejects_invalid_mcp_json(tmp_path: Path) -> None:
    settings = load_settings(
        env={
            "MEMORY_PATH": str(tmp_path),
            "CHAT_BACKEND": "local",
            "MCP_SERVERS_JSON": "{",
        }
    ).unwrap()
    error = assert_err(
        Llm.from_settings(
            settings,
            store=Store(tmp_path / "chats.db", tmp_path / "core.md"),
            logger=NOOP_LOGGER,
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="valid JSON",
    )
    assert "MCP_SERVERS_JSON" in error.message


@pytest.mark.anyio
async def test_external_fastmcp_tool_runs_and_keeps_stable_prefix(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        {
            "divergence": {
                "command": sys.executable,
                "args": [str(_SERVER)],
                "cwd": str(_REPO),
            }
        },
    )
    store = Store(settings.db_path, settings.core_memory_path)
    llm = Llm.from_settings(settings, store=store, logger=NOOP_LOGGER).unwrap()
    engine = CharacterEngine(store, llm, settings, DEFAULT_PERSONA, NOOP_LOGGER)
    try:
        await engine.start()
        await engine.submit_message("c", "こんにちは")
        before = (await engine.context_snapshot("c")).unwrap()
        reply = (await engine.submit_message("c", "連想して: 海")).unwrap()
        after = (await engine.context_snapshot("c")).unwrap()
        assert "external-fastmcp" in reply.reply
        assert "波" in reply.reply
        assert after.stable_prefix == before.stable_prefix
        assert after.stable_prefix_sha256 == before.stable_prefix_sha256
        assert "External associative divergence" not in after.stable_prefix
        assert after.epoch > before.epoch
    finally:
        await engine.aclose()


def test_parse_mcp_servers_reports_validation_error_kind() -> None:
    result = parse_mcp_servers("[]")
    assert isinstance(result, Err)
    assert is_ok(parse_mcp_servers(None))
