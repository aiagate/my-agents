import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Self

from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from flow_res import Err, Ok, Result
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_ai.mcp import MCPToolset

from my_agents.core.types import AppError, AppErrorKind, SecretStr


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, SecretStr], ...] = ()
    cwd: Path | None = None
    url: SecretStr | None = None
    headers: tuple[tuple[str, SecretStr], ...] = ()


class _ServerInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, SecretStr] = Field(default_factory=dict)
    cwd: str | None = None
    url: SecretStr | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_source(self) -> Self:
        if self.url is not None:
            if (
                self.command is not None
                or self.args
                or self.env
                or self.cwd is not None
            ):
                raise ValueError("must use either 'url' or 'command'.")
            if not self.url.get_secret_value().startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://.")
            return self
        if self.headers:
            raise ValueError("headers require 'url'.")
        if not self.command:
            raise ValueError("requires 'command' or 'url'.")
        return self

    def to_spec(self, name: str) -> McpServerSpec:
        if self.url is not None:
            return McpServerSpec(
                name=name,
                url=self.url,
                headers=tuple(self.headers.items()),
            )
        return McpServerSpec(
            name=name,
            command=self.command or "",
            args=self.args,
            env=tuple(self.env.items()),
            cwd=None if self.cwd is None else Path(self.cwd).expanduser(),
        )


def _invalid(message: str) -> AppError:
    return AppError(AppErrorKind.VALIDATION, message)


def _field_error(name: str, exc: ValidationError) -> AppError:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "entry"
    message = first["msg"].removeprefix("Value error, ")
    return _invalid(f"Invalid MCP server '{name}' field '{location}': {message}")


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
            servers.append(_ServerInput.model_validate(entry).to_spec(name))
        except ValidationError as exc:
            return Err(_field_error(name, exc))
    return Ok(tuple(servers))


SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "VIRTUAL_ENV",
    "UV_CACHE_DIR",
)


def _environment(
    server: McpServerSpec, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    source = environ if base is None else base
    inherited = {key: source[key] for key in SAFE_ENV_KEYS if key in source}
    inherited.update({name: value.get_secret_value() for name, value in server.env})
    return inherited


def _toolset(
    server: McpServerSpec, base: Mapping[str, str] | None = None
) -> MCPToolset[None]:
    transport = (
        StreamableHttpTransport(
            server.url.get_secret_value(),
            headers={name: value.get_secret_value() for name, value in server.headers},
        )
        if server.url is not None
        else StdioTransport(
            server.command,
            list(server.args),
            env=_environment(server, base),
            cwd=None if server.cwd is None else str(server.cwd),
        )
    )
    return MCPToolset(transport, id=server.name, include_instructions=False)


def build_toolsets(
    servers: Sequence[McpServerSpec],
    *,
    base_env: Mapping[str, str] | None = None,
) -> tuple[MCPToolset[None], ...]:
    return tuple(_toolset(server, base_env) for server in servers)
