from pathlib import Path

from my_agents.presentation.cli.args import (
    CliArgsError,
    CliHelp,
    CliOptions,
    parse_cli_args,
)


def test_parse_cli_args_defaults() -> None:
    result = parse_cli_args(())

    assert result == CliOptions(debug=False, log_file=None)


def test_parse_cli_args_with_debug_and_log_file() -> None:
    result = parse_cli_args(("--debug", "--log-file", "logs/custom.jsonl"))

    assert result == CliOptions(debug=True, log_file=Path("logs/custom.jsonl"))


def test_parse_cli_args_help_returns_cli_help() -> None:
    long_help = parse_cli_args(("--help",))
    short_help = parse_cli_args(("-h",))

    assert isinstance(long_help, CliHelp)
    assert "--debug" in long_help.text
    assert "--log-file" in long_help.text
    assert short_help == long_help


def test_parse_cli_args_invalid_options_return_cli_args_error() -> None:
    unknown = parse_cli_args(("--unknown-flag",))
    missing_value = parse_cli_args(("--log-file",))

    assert isinstance(unknown, CliArgsError)
    assert "--unknown-flag" in unknown.message
    assert isinstance(missing_value, CliArgsError)
    assert "--log-file" in missing_value.message
