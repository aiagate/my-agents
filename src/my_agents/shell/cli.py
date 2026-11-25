import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from flow_res import is_err

from my_agents.core.types import CharacterPersona
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import Llm
from my_agents.shell.logger import (
    NOOP_LOGGER,
    IAppLogger,
    JsonlLogger,
    LogEntry,
    LogLevel,
)
from my_agents.shell.server import serve
from my_agents.shell.settings import Settings, load_persona, load_settings
from my_agents.shell.store import Store


def _write_error(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="my-agents")
    parser.add_argument("command", nargs="?", choices=["serve"], default=None)
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--debug", action="store_true")
    return parser


def _try_logger(
    path: Path | None, write_error: Callable[[str], None], *, explicit: bool
) -> IAppLogger | None:
    if path is None:
        return NOOP_LOGGER
    try:
        return JsonlLogger.open(path)
    except OSError as exc:
        if explicit:
            write_error(f"Error: cannot open log file: {exc}")
            return None
        write_error(f"Warning: cannot open log file: {exc}")
        return NOOP_LOGGER


def _engine(
    settings: Settings,
    persona: CharacterPersona,
    logger: IAppLogger,
) -> CharacterEngine:
    store = Store(settings.db_path, settings.core_memory_path)
    llm = Llm.from_settings(settings, store=store, logger=logger)
    if is_err(llm):
        raise RuntimeError(llm.error.message)
    return CharacterEngine(store, llm.value, settings, persona, logger)


def _repl(
    engine: CharacterEngine,
    read_input: Callable[[str], str],
    write_output: Callable[[str], None],
) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(engine.start())
        write_output("my-agents local chat. exit / quit で終了します。")
        while True:
            try:
                text = read_input("You > ")
            except EOFError:
                return
            if text.strip().lower() in {"exit", "quit"}:
                return
            result = loop.run_until_complete(engine.submit_message("default", text))
            if is_err(result):
                write_output(f"Error: {result.error.message}")
            else:
                write_output(f"AI > {result.value.reply}")
    finally:
        loop.run_until_complete(engine.aclose())
        loop.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    read_input: Callable[[str], str] | None = None,
    write_output: Callable[[str], None] = print,
    write_error: Callable[[str], None] = _write_error,
) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code is None else int(exc.code)
    logger: IAppLogger = NOOP_LOGGER
    explicit_logger = _try_logger(arguments.log_file, write_error, explicit=True)
    if explicit_logger is None:
        return 1
    logger = explicit_logger
    try:
        loaded = load_settings(env=env, env_path=Path.cwd() / ".env")
        if is_err(loaded):
            logger.log(
                LogEntry(
                    LogLevel.ERROR,
                    "settings_load_failed",
                    loaded.error.message,
                )
            )
            write_error(f"Error: {loaded.error.message}")
            return 1
        settings = loaded.value
        if arguments.log_file is None and settings.log_file is not None:
            logger.close()
            logger = (
                _try_logger(settings.log_file, write_error, explicit=False)
                or NOOP_LOGGER
            )
        if arguments.debug:
            resolved_path = arguments.log_file or settings.log_file
            write_error(
                "Debug: log file = "
                f"{resolved_path if resolved_path is not None else '<none>'}"
            )
        persona = load_persona(settings.persona_path)
        if is_err(persona):
            write_error(f"Error: {persona.error.message}")
            return 1
        try:
            engine = _engine(settings, persona.value, logger)
        except RuntimeError as exc:
            write_error(f"Error: {exc}")
            return 1
        if arguments.command == "serve":
            write_output(
                f"my-agents listening on http://{settings.server_host}:{settings.server_port}"
            )
            serve(engine, settings.server_host, settings.server_port)
            return 0
        _repl(engine, read_input or input, write_output)
        logger.log(LogEntry(LogLevel.INFO, "cli_exit", "session finished"))
        return 0
    finally:
        logger.close()
