import json
from pathlib import Path

from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from flow_res import Ok
from pydantic import SecretStr

from my_agents.core.types import AppErrorKind
from my_agents.shell.mcp import (
    McpServerSpec,
    build_toolsets,
    parse_mcp_servers,
)
from tests.helpers import assert_err


def test_parse_mcp_servers_handles_none_stdio_and_http() -> None:
    empty = parse_mcp_servers(None)
    assert isinstance(empty, Ok)
    assert empty.value == ()

    raw = json.dumps(
        {
            "local": {
                "command": "python",
                "args": ["-m", "demo"],
                "env": {"TOOL_KEY": "tsec"},
                "cwd": "/tmp",
            },
            "remote": {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer hsec"},
            },
        }
    )
    parsed = parse_mcp_servers(raw)
    assert isinstance(parsed, Ok)
    stdio, http = parsed.value
    assert (stdio.name, stdio.command, stdio.args, stdio.cwd) == (
        "local",
        "python",
        ("-m", "demo"),
        Path("/tmp"),
    )
    assert stdio.env[0][1].get_secret_value() == "tsec"
    assert http.url is not None
    assert http.url.get_secret_value() == "https://example.com/mcp"
    assert http.headers[0][1].get_secret_value() == "Bearer hsec"


def test_parse_mcp_servers_rejects_malformed_configs() -> None:
    cases = [
        ("{", "valid JSON"),
        ("[]", "JSON object"),
        ('{"": {}}', "non-empty strings"),
        ('{"bad": 123}', "must be a JSON object"),
        ('{"bad": {"command": "py", "extra": 1}}', "Invalid MCP server 'bad'"),
        (
            '{"bad": {"url": "https://x", "command": "py"}}',
            "either 'url' or 'command'",
        ),
        ('{"bad": {"url": "ftp://x"}}', "http:// or https://"),
        ('{"bad": {"headers": {"X-A": "1"}}}', "headers require 'url'"),
        ('{"bad": {}}', "requires 'command' or 'url'"),
    ]
    for raw, needle in cases:
        assert_err(
            parse_mcp_servers(raw),
            kind=AppErrorKind.VALIDATION,
            message_contains=needle,
        )


def test_build_toolsets_isolates_stdio_env_and_disables_instructions() -> None:
    specs = (
        McpServerSpec(
            name="stdio_srv",
            command="python",
            args=("-V",),
            env=(("API_TOKEN", SecretStr("child-secret")),),
            cwd=Path("/tmp"),
        ),
        McpServerSpec(
            name="http_srv",
            url=SecretStr("https://mcp.example.com"),
            headers=(("X-Key", SecretStr("hdr-secret")),),
        ),
    )
    base_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LLM_API_KEY": "must-not-leak-to-mcp-child",
    }
    toolsets = build_toolsets(specs, base_env=base_env)
    assert len(toolsets) == 2

    stdio_ts, http_ts = toolsets
    assert stdio_ts.include_instructions is False
    assert isinstance(stdio_ts.client.transport, StdioTransport)
    assert stdio_ts.client.transport.env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "API_TOKEN": "child-secret",
    }
    assert stdio_ts.client.transport.cwd == "/tmp"

    assert http_ts.include_instructions is False
    assert isinstance(http_ts.client.transport, StreamableHttpTransport)
    assert http_ts.client.transport.headers == {"X-Key": "hdr-secret"}
