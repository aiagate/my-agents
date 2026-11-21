from collections.abc import Callable, Mapping
from pathlib import Path

from flow_res import Err, Ok, Result, is_err

from my_agents.infrastructure.pydantic_ai_runner import PydanticAIAgentRunner
from my_agents.infrastructure.settings import SettingsError, load_app_settings
from my_agents.presentation.cli.repl import CliRepl, write_stderr
from my_agents.usecases.send_message import SendMessageHandler


def build_cli(
    *,
    env: Mapping[str, str] | None = None,
    env_path: str | Path | None = None,
    read_input: Callable[[str], str] = input,
    write_output: Callable[[str], None] = print,
    write_error: Callable[[str], None] = write_stderr,
) -> Result[CliRepl, SettingsError]:
    resolved_env_path = Path(env_path) if env_path is not None else Path.cwd() / ".env"

    settings_result = load_app_settings(env=env, env_path=resolved_env_path)
    if is_err(settings_result):
        return Err(settings_result.error)

    agent_runner = PydanticAIAgentRunner.from_settings(settings_result.value)
    send_message = SendMessageHandler(agent_runner)
    return Ok(
        CliRepl(
            send_message,
            read_input=read_input,
            write_output=write_output,
            write_error=write_error,
        )
    )


def run_cli(
    *,
    env: Mapping[str, str] | None = None,
    env_path: str | Path | None = None,
    read_input: Callable[[str], str] = input,
    write_output: Callable[[str], None] = print,
    write_error: Callable[[str], None] = write_stderr,
) -> int:
    result = build_cli(
        env=env,
        env_path=env_path,
        read_input=read_input,
        write_output=write_output,
        write_error=write_error,
    )
    if is_err(result):
        write_error(f"Error: {result.error.message}")
        return 1

    result.value.run()
    return 0
