import json
from collections.abc import Sequence
from dataclasses import dataclass
from os import environ
from pathlib import Path

from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from flow_res import Err, Ok, Result
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai.mcp import MCPToolset

from my_agents.core.types import AppError, AppErrorKind


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: Path | None = None
    url: str | None = None


class _ServerInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None


def _invalid(message: str) -> AppError:
    return AppError(AppErrorKind.VALIDATION, message)


def _field_error(name: str, exc: ValidationError) -> AppError:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "entry"
    return _invalid(f"Invalid MCP server '{name}' field '{location}': {first['msg']}")


def _convert(name: str, parsed: _ServerInput) -> Result[McpServerSpec, AppError]:
    if parsed.url is not None:
        if (
            parsed.command is not None
            or parsed.args
            or parsed.env
            or parsed.cwd is not None
        ):
            return Err(
                _invalid(f"MCP server '{name}' must use either 'url' or 'command'.")
            )
        if not parsed.url.startswith(("http://", "https://")):
            return Err(
                _invalid(
                    f"MCP server '{name}' url must start with http:// or https://."
                )
            )
        return Ok(McpServerSpec(name=name, url=parsed.url))
    if not parsed.command:
        return Err(_invalid(f"MCP server '{name}' requires 'command' or 'url'."))
    return Ok(
        McpServerSpec(
            name=name,
            command=parsed.command,
            args=parsed.args,
            env=tuple(parsed.env.items()),
            cwd=None if parsed.cwd is None else Path(parsed.cwd).expanduser(),
        )
    )


def parse_mcp_servers(raw: str | None) -> Result[tuple[McpServerSpec, ...], AppError]:
    if raw is None:
        return Ok(())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Err(_invalid(f"MCP_SERVERS_JSON must be valid JSON: {exc.msg}"))
    if not isinstance(data, dict):
        return Err(_invalid("MCP_SERVERS_JSON must be a JSON object."))
    servers: list[McpServerSpec] = []
    for name, entry in data.items():
        if not isinstance(name, str) or not name:
            return Err(_invalid("MCP server names must be non-empty strings."))
        if not isinstance(entry, dict):
            return Err(_invalid(f"MCP server '{name}' must be a JSON object."))
        try:
            parsed = _ServerInput.model_validate(entry)
        except ValidationError as exc:
            return Err(_field_error(name, exc))
        converted = _convert(name, parsed)
        if isinstance(converted, Err):
            return converted
        servers.append(converted.value)
    return Ok(tuple(servers))


def _environment(server: McpServerSpec) -> dict[str, str] | None:
    if not server.env:
        return None
    return {**environ, **dict(server.env)}


def _toolset(server: McpServerSpec) -> MCPToolset[None]:
    if server.url is not None:
        return MCPToolset(
            StreamableHttpTransport(server.url),
            id=server.name,
            include_instructions=False,
        )
    return MCPToolset(
        StdioTransport(
            server.command,
            list(server.args),
            env=_environment(server),
            cwd=None if server.cwd is None else str(server.cwd),
        ),
        id=server.name,
        include_instructions=False,
    )


def build_toolsets(
    servers: Sequence[McpServerSpec],
) -> tuple[MCPToolset[None], ...]:
    return tuple(_toolset(server) for server in servers)
