from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path


class _CliNamespace(Namespace):
    debug: bool = False
    log_file: Path | None = None


@dataclass(frozen=True, slots=True)
class CliOptions:
    debug: bool = False
    log_file: Path | None = None


@dataclass(frozen=True, slots=True)
class CliHelp:
    text: str


@dataclass(frozen=True, slots=True)
class CliArgsError:
    message: str


type CliParseResult = CliOptions | CliHelp | CliArgsError


def parse_cli_args(argv: Sequence[str]) -> CliParseResult:
    parser = ArgumentParser(prog="my-agents")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="エラーの詳細情報を標準エラー出力に表示します。",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="ログファイルの出力先パスを指定します。",
    )

    stdout_buffer = StringIO()
    stderr_buffer = StringIO()
    namespace = _CliNamespace()
    try:
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            parsed = parser.parse_args(list(argv), namespace=namespace)
    except SystemExit as exc:
        if exc.code == 0:
            return CliHelp(text=stdout_buffer.getvalue().rstrip("\n"))
        return CliArgsError(message=stderr_buffer.getvalue().rstrip("\n"))

    return CliOptions(debug=parsed.debug, log_file=parsed.log_file)
