from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from flow_res import Err, Ok, Result
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_ai.mcp import MCPToolset

from my_agents.core.types import AppError, AppErrorKind, SecretStr


@dataclass(frozen=True, slots=True)
class StdioMcpServer:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, SecretStr], ...] = ()
    cwd: Path | None = None


@dataclass(frozen=True, slots=True)
class HttpMcpServer:
    name: str
    url: SecretStr
    headers: tuple[tuple[str, SecretStr], ...] = ()


type McpServerSpec = StdioMcpServer | HttpMcpServer


class _ServerInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, SecretStr] = Field(default_factory=dict)
    cwd: str | None = None
    url: SecretStr | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_source(self) -> Self:
        if self.url is not None:
            if self.command or self.args or self.env or self.cwd is not None:
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
            return HttpMcpServer(
                name=name,
                url=self.url,
                headers=tuple(self.headers.items()),
            )
        return StdioMcpServer(
            name=name,
            command=self.command,
            args=self.args,
            env=tuple(self.env.items()),
            cwd=None if self.cwd is None else Path(self.cwd).expanduser(),
        )


_SERVERS = TypeAdapter(dict[Annotated[str, Field(min_length=1)], _ServerInput])


def _invalid(message: str) -> AppError:
    return AppError(AppErrorKind.VALIDATION, message)


def _validation_error(exc: ValidationError) -> AppError:
    first = exc.errors()[0]
    location = first["loc"]
    message = first["msg"].removeprefix("Value error, ")
    if not location:
        if first["type"] == "json_invalid":
            return _invalid(f"MCP_SERVERS_JSON must be valid JSON: {message}")
        return _invalid("MCP_SERVERS_JSON must be a JSON object.")
    name = str(location[0])
    if not name:
        return _invalid("MCP server names must be non-empty strings.")
    if len(location) == 1:
        return _invalid(f"Invalid MCP server '{name}': {message}")
    field = ".".join(str(part) for part in location[1:])
    return _invalid(f"Invalid MCP server '{name}' field '{field}': {message}")


def parse_mcp_servers(raw: str | None) -> Result[tuple[McpServerSpec, ...], AppError]:
    if raw is None:
        return Ok(())
    try:
        data = _SERVERS.validate_json(raw)
    except ValidationError as exc:
        return Err(_validation_error(exc))
    servers: list[McpServerSpec] = []
    for name, entry in data.items():
        try:
            servers.append(entry.to_spec(name))
        except (OSError, RuntimeError) as exc:
            return Err(_invalid(f"Invalid MCP server '{name}' field 'cwd': {exc}"))
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


def _environment(server: StdioMcpServer, base: Mapping[str, str]) -> dict[str, str]:
    inherited = {key: base[key] for key in SAFE_ENV_KEYS if key in base}
    inherited.update({name: value.get_secret_value() for name, value in server.env})
    return inherited


def _toolset(server: McpServerSpec, base: Mapping[str, str]) -> MCPToolset[None]:
    if isinstance(server, StdioMcpServer):
        transport = StdioTransport(
            server.command,
            list(server.args),
            env=_environment(server, base),
            cwd=None if server.cwd is None else str(server.cwd),
        )
    else:
        transport = StreamableHttpTransport(
            server.url.get_secret_value(),
            headers={name: value.get_secret_value() for name, value in server.headers},
        )
    return MCPToolset(transport, id=server.name, include_instructions=False)


def build_toolsets(
    servers: Sequence[McpServerSpec],
    *,
    base_env: Mapping[str, str],
) -> tuple[MCPToolset[None], ...]:
    return tuple(_toolset(server, base_env) for server in servers)
