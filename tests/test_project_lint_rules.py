import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from tools.project_lint.rules import (
    check_assert_error_message,
    check_exhaustive_branch,
    check_immutable_frozen_dataclass_fields,
    check_layer_boundaries,
    check_no_cross_layer_internal_monkeypatch,
    check_no_direct_logging,
    check_no_docstrings,
    check_no_exception_result_errors,
    check_no_global_environ_mutation,
    check_no_inline_suppressions,
    check_no_plain_str_secrets,
    check_no_private_dataclass_init_fields,
    check_no_private_type_in_public_api,
    check_no_shallow_tests,
    check_strict_coverage_config,
    check_strict_ty_config,
    check_thin_entrypoint,
    check_tool_descriptions,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).removeprefix("\n"), encoding="utf-8")
    return path


def _package(root: Path) -> Path:
    return root / "src" / "my_agents"


def _ruff_check(directory: Path) -> subprocess.CompletedProcess[str]:
    ruff = shutil.which("ruff") or str(Path(sys.executable).with_name("ruff"))
    return subprocess.run(
        [ruff, "check", "--no-cache", "--output-format", "concise", "."],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


def test_pl001_flags_plain_str_secret_and_allows_vo_and_non_secrets(
    tmp_path: Path,
) -> None:
    src = _package(tmp_path)
    _write(
        src / "contracts" / "secret.py",
        """
        from dataclasses import dataclass, field
        class SecretStr:
            __slots__ = ('_value',)
            def __init__(self, value: str, /) -> None:
                self._value = value
            def __repr__(self) -> str:
                return 'SecretStr(***)'
            def __str__(self) -> str:
                return '***'
            def __eq__(self, other: object) -> bool:
                return isinstance(other, SecretStr) and self._value == other._value
            def __hash__(self) -> int:
                return hash(self._value)
            def get_secret_value(self) -> str:
                return self._value
        @dataclass(frozen=True)
        class DataclassSecret:
            value: str = field(repr=False)
            def __repr__(self) -> str:
                return '***'
            def __str__(self) -> str:
                return '***'
            def __eq__(self, other: object) -> bool:
                return False
            def __hash__(self) -> int:
                return 0
        class IncompleteSecret:
            def __init__(self, value: str, /) -> None:
                self.value = value
        """,
    )
    _write(
        src / "infrastructure" / "bad.py",
        """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class BadConfig:
            api_key: str
            auth_token: str | None
            max_tokens: int
            token_count: str
            key_name: str
        """,
    )
    _write(
        src / "infrastructure" / "good.py",
        """
        from dataclasses import dataclass
        from my_agents.contracts.secret import SecretStr
        @dataclass(frozen=True)
        class GoodConfig:
            api_key: SecretStr
            auth_token: SecretStr | None
        """,
    )

    findings = check_no_plain_str_secrets(src)

    assert len(findings) == 6
    assert {f.rule for f in findings} == {"PL001"}
    messages = " ".join(f.message for f in findings)
    assert "api_key" in messages
    assert "auth_token" in messages
    assert "DataclassSecret" in messages
    assert "IncompleteSecret" in messages
    assert "SecretStr" not in messages


def test_pl002_requires_error_message_check_with_error_type(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    _write(
        tests_dir / "test_sample.py",
        """
        def test_bad() -> None:
            error = object()
            assert error.type == 1
        def test_good_direct() -> None:
            error = object()
            assert error.type == 1
            assert 'boom' in error.message
        def test_good_helper() -> None:
            assert_err(object(), expected_type=1, message_contains='boom')
        """,
    )

    findings = check_assert_error_message(tests_dir)

    assert len(findings) == 1
    assert findings[0].rule == "PL002"
    assert "test_bad" in findings[0].message


def test_pl003_flags_non_exhaustive_if_else_on_literal_and_enum(
    tmp_path: Path,
) -> None:
    src = _package(tmp_path)
    _write(
        src / "contracts" / "types.py",
        """
        from dataclasses import dataclass
        from enum import Enum, auto
        from typing import Literal
        class Mode(Enum):
            FAST = auto()
            SLOW = auto()
        @dataclass(frozen=True)
        class Msg:
            role: Literal['user', 'assistant']
        """,
    )
    _write(
        src / "infrastructure" / "sample.py",
        """
        from typing import assert_never
        def bad_literal(msg: object) -> int:
            if msg.role == 'user':
                return 1
            else:
                return 2
        def bad_enum(mode: object) -> int:
            if mode is Mode.FAST:
                return 1
            else:
                return 2
        def good_plain_str(prompt: str) -> int:
            if prompt == 'quit':
                return 0
            else:
                return 1
        def good_match(msg: object) -> int:
            match msg.role:
                case 'user':
                    return 1
                case 'assistant':
                    return 2
                case _:
                    assert_never(msg.role)
        """,
    )

    findings = check_exhaustive_branch(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL003"}


def test_pl003_regression_ternary_ifexp_on_closed_literal(tmp_path: Path) -> None:
    # 2026-11-22 | thread mcp:launch:1a4040d4c0a04b9a800ad4bbe24f3bfa7c6c26d232c6018cca491c9cb0b465e0 | SHA 83ff9e4
    sample = _write(
        tmp_path / "sample.py",
        """
        import typing
        from enum import Enum, auto
        from typing import Literal, assert_never
        class ChatTurn:
            role: Literal['user', 'assistant']
        class Mode(Enum):
            FAST = auto()
            SLOW = auto()
        def labels(turn: ChatTurn, mode: Mode, prompt: str) -> None:
            label = "You" if turn.role == "user" else "AI"
            speed = 1 if mode is Mode.FAST else 2
            chained = 1 if mode is Mode.FAST else 2 if mode is Mode.SLOW else 0
            guarded = 1 if mode is Mode.FAST else assert_never(mode)
            qualified = 1 if mode is Mode.FAST else (
                2 if mode is Mode.SLOW else typing.assert_never(mode)
            )
            plain = 1 if prompt == 'quit' else 0
            flag = 1 if prompt else 0
        """,
    )

    findings = check_exhaustive_branch(tmp_path)

    assert {(finding.rule, finding.path) for finding in findings} == {("PL003", sample)}
    assert [(finding.line, finding.col, finding.message) for finding in findings] == [
        (
            10,
            13,
            "Non-exhaustive '... if ... else ...' expression on closed type 'role'.",
        ),
        (
            11,
            13,
            "Non-exhaustive '... if ... else ...' expression on closed type 'Mode'.",
        ),
        (
            12,
            15,
            "Non-exhaustive '... if ... else ...' expression on closed type 'Mode'.",
        ),
    ]


def test_pl004_enforces_two_layer_boundaries(tmp_path: Path) -> None:
    src = _package(tmp_path)
    _write(
        src / "core" / "bad.py",
        """
        import sqlite3
        import pydantic_ai
        from my_agents.shell.store import Store
        """,
    )
    _write(src / "core" / "ok.py", "import pydantic\n")
    _write(
        src / "shell" / "ok.py",
        "import sqlite3\nfrom my_agents.core.types import AppError\n",
    )
    _write(src / "__init__.py", "from my_agents.shell.server import main\n")

    findings = check_layer_boundaries(src)

    assert len(findings) == 3
    assert {f.rule for f in findings} == {"PL004"}
    messages = " ".join(f.message for f in findings)
    assert "external I/O library 'sqlite3'" in messages
    assert "external I/O library 'pydantic_ai'" in messages
    assert "must not import from 'shell'" in messages


def test_pl005_flags_weakened_ty_config(tmp_path: Path) -> None:
    bad_toml = _write(
        tmp_path / "bad.toml",
        """
        [tool.ty.terminal]
        error-on-warning = false
        [tool.ty.rules]
        invalid-assignment = "ignore"
        """,
    )
    good_toml = _write(
        tmp_path / "good.toml",
        """
        [tool.ty.terminal]
        error-on-warning = true
        """,
    )

    bad_findings = check_strict_ty_config(bad_toml)
    good_findings = check_strict_ty_config(good_toml)

    assert len(bad_findings) == 2
    assert {f.rule for f in bad_findings} == {"PL005"}
    assert good_findings == []


def test_pl006_flags_docstrings_and_allows_runtime_decorator_exceptions(
    tmp_path: Path,
) -> None:
    src = _package(tmp_path)
    _write(
        src / "bad.py",
        '''
        """Module docstring."""
        class Sample:
            """Class docstring."""
            value: int
            """Attribute docstring."""
            def method(self) -> str:
                """Method docstring."""
                return "ok"
        async def fetch() -> str:
            """Async docstring."""
            return "ok"
        ''',
    )
    _write(
        src / "good.py",
        """
        from abc import ABC, abstractmethod
        class Port(ABC):
            @abstractmethod
            async def run(self) -> str: ...
        def greet() -> str:
            message = "hello"
            return message
        """,
    )
    _write(
        src / "runtime_tool.py",
        '''
        @agent.tool
        def lookup_weather(city: str) -> str:
            """Look up current weather for a city."""
            return city
        @other.decorator
        def regular_helper() -> str:
            """Unallowed docstring."""
            return "ok"
        ''',
    )

    default_findings = check_no_docstrings((src,))
    assert len(default_findings) == 7
    assert {f.rule for f in default_findings} == {"PL006"}
    assert all(f.path.name != "good.py" for f in default_findings)

    allowed_findings = check_no_docstrings(
        (src,),
        allowed_decorators=frozenset({"agent.tool"}),
    )
    assert len(allowed_findings) == 6
    assert {f.rule for f in allowed_findings} == {"PL006"}
    assert all("lookup_weather" not in f.message for f in allowed_findings)
    assert any("regular_helper" in f.message for f in allowed_findings)


def test_pl007_flags_weakened_coverage_config(tmp_path: Path) -> None:
    bad_toml = _write(
        tmp_path / "bad.toml",
        """
        [tool.coverage.run]
        branch = false
        omit = ["src/my_agents/__init__.py", "src/my_agents/infrastructure/a.py"]
        [tool.coverage.report]
        fail_under = 80
        exclude_also = ["if TYPE_CHECKING:", "raise NotImplementedError"]
        """,
    )
    good_toml = _write(
        tmp_path / "good.toml",
        """
        [tool.coverage.run]
        branch = true
        omit = ["src/my_agents/__init__.py"]
        [tool.coverage.report]
        fail_under = 100
        exclude_also = ["if TYPE_CHECKING:", "assert_never\\\\("]
        """,
    )

    bad_findings = check_strict_coverage_config(bad_toml)
    good_findings = check_strict_coverage_config(good_toml)

    assert len(bad_findings) == 4
    assert {f.rule for f in bad_findings} == {"PL007"}
    assert good_findings == []


def test_pl008_flags_inline_suppressions_and_ignores_strings(tmp_path: Path) -> None:
    src = _package(tmp_path)
    _write(
        src / "bad.py",
        """
        a = 1  # type: ignore[assignment]
        b = 2  # ty: ignore[invalid-argument-type]
        c = 3  # noqa: TID251
        d = 4  # pragma: no cover
        """,
    )
    _write(
        src / "good.py",
        """
        text = "# noqa: TID251 # type: ignore # pragma: no cover"
        value = 1  # regular why note
        """,
    )

    findings = check_no_inline_suppressions((src,))

    assert len(findings) == 4
    assert {f.rule for f in findings} == {"PL008"}
    assert all(f.path.name == "bad.py" for f in findings)


def test_tid251_regression_cast_and_any_banned_repo_wide(tmp_path: Path) -> None:
    # Replaces PL009 (retired 2026-11-26, thread 8019e06b, base 5de388f): cast is
    # banned repo-wide, including tests/ and tools/, not only src/.
    shutil.copy(_PROJECT_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    _write(
        tmp_path / "tests" / "cast_from.py",
        """
        from typing import cast

        _ = cast(int, 1)
        """,
    )
    _write(
        tmp_path / "tests" / "cast_alias.py",
        """
        from typing import cast as c

        _ = c(int, 1)
        """,
    )
    _write(
        tmp_path / "tests" / "cast_module.py",
        """
        import typing

        _ = typing.cast(int, 1)
        """,
    )
    _write(
        tmp_path / "tests" / "cast_module_alias.py",
        """
        import typing as t

        _ = t.cast(int, 1)
        """,
    )
    _write(
        tmp_path / "tests" / "cast_typing_extensions.py",
        """
        from typing_extensions import cast

        _ = cast(int, 1)
        """,
    )
    _write(
        tmp_path / "tests" / "any_use.py",
        """
        from typing import Any
        from typing_extensions import Any as ExtAny

        _ = Any
        _ = ExtAny
        """,
    )
    _write(
        tmp_path / "tools" / "any_use.py",
        """
        from typing import Any
        from typing_extensions import Any as ExtAny

        _ = Any
        _ = ExtAny
        """,
    )

    result = _ruff_check(tmp_path)

    violations = [line for line in result.stdout.splitlines() if "TID251" in line]
    cast_violations = [
        line
        for line in violations
        if "typing.cast" in line or "typing_extensions.cast" in line
    ]
    any_violations = [
        line
        for line in violations
        if "typing.Any" in line or "typing_extensions.Any" in line
    ]
    assert result.returncode == 1
    assert len(violations) == 9
    assert len(any_violations) == 4
    assert {line.split(":")[0] for line in cast_violations} == {
        "tests/cast_alias.py",
        "tests/cast_from.py",
        "tests/cast_module.py",
        "tests/cast_module_alias.py",
        "tests/cast_typing_extensions.py",
    }
    assert all(
        "narrow types or fix the annotation instead" in line for line in cast_violations
    )
    assert {line.split(":")[0] for line in any_violations} == {
        "tests/any_use.py",
        "tools/any_use.py",
    }


def test_pl010_flags_shallow_or_assertionless_tests(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    _write(
        tests_dir / "test_sample.py",
        """
        def test_only_isinstance() -> None:
            value = object()
            assert isinstance(value, object)
        def test_only_not_none() -> None:
            value = object()
            assert value is not None
        def test_no_assert() -> None:
            _ = 1 + 1
        def test_good_equality() -> None:
            value = 1 + 1
            assert isinstance(value, int)
            assert value == 2
        def test_good_raises() -> None:
            with pytest.raises(ValueError):
                int('bad')
        def test_good_helper() -> None:
            assert_err(object(), message_contains='boom')
        """,
    )

    findings = check_no_shallow_tests(tests_dir)

    assert len(findings) == 3
    assert {f.rule for f in findings} == {"PL010"}
    messages = " ".join(f.message for f in findings)
    assert "test_only_isinstance" in messages
    assert "test_only_not_none" in messages
    assert "test_no_assert" in messages


def test_pl011_flags_private_dataclass_init_fields(tmp_path: Path) -> None:
    src = _package(tmp_path)
    _write(
        src / "sample.py",
        """
        from dataclasses import dataclass, field
        from typing import ClassVar
        @dataclass(frozen=True, slots=True)
        class BadSecret:
            _value: str
        @dataclass(frozen=True, slots=True)
        class GoodPublicField:
            value: str = field(repr=False)
            _cache: int = field(default=0, init=False)
            _tag: ClassVar[str] = 'ok'
        @dataclass(frozen=True, slots=True, init=False)
        class GoodExplicitInit:
            _value: str
            def __init__(self, value: str, /) -> None:
                object.__setattr__(self, '_value', value)
        """,
    )

    findings = check_no_private_dataclass_init_fields(src)

    assert len(findings) == 1
    assert findings[0].rule == "PL011"
    assert "BadSecret" in findings[0].message
    assert "_value" in findings[0].message


def test_pl002_requires_display_message_check_for_displayable_error(
    tmp_path: Path,
) -> None:
    src = _package(tmp_path)
    tests_dir = tmp_path / "tests"
    _write(
        src / "usecases" / "errors.py",
        """
        from dataclasses import dataclass
        from enum import Enum, auto
        class UseCaseErrorType(Enum):
            INVALID = auto()
        @dataclass(frozen=True, slots=True)
        class UseCaseError:
            type: UseCaseErrorType
            message: str
            display_message: str
        """,
    )
    _write(
        tests_dir / "test_usecase.py",
        """
        def test_bad_missing_display_check() -> None:
            assert_err(object(), expected_type=UseCaseErrorType.INVALID, message_contains='bad')
        def test_good_helper_display_check() -> None:
            assert_err(
                object(),
                expected_type=UseCaseErrorType.INVALID,
                message_contains='bad',
                display_message_contains='user msg',
            )
        def test_good_attr_display_check() -> None:
            error = assert_err(
                object(),
                expected_type=UseCaseErrorType.INVALID,
                message_contains='bad',
            )
            assert error.display_message == 'user msg'
        def test_ignored_fake_instantiation_without_assert() -> None:
            fake_err = UseCaseError(
                type=UseCaseErrorType.INVALID,
                message='internal',
                display_message='user msg',
            )
            assert fake_err.display_message == 'user msg'
        """,
    )

    findings = check_assert_error_message(tests_dir)

    assert len(findings) == 1
    assert findings[0].rule == "PL002"
    assert "test_bad_missing_display_check" in findings[0].message


def test_pl014_enforces_thin_entrypoint(tmp_path: Path) -> None:
    bad_src = tmp_path / "bad" / "src" / "my_agents"
    _write(
        bad_src / "__init__.py",
        """
        import os
        from .shell.server import main as _main
        def main() -> int:
            if os.environ.get('DEBUG'):
                return 1
            return _main()
        """,
    )
    good_src = tmp_path / "good" / "src" / "my_agents"
    _write(
        good_src / "__init__.py",
        """
        from my_agents.shell.server import main as _main
        def main() -> int:
            return _main()
        """,
    )
    wrong_import = tmp_path / "wrong" / "src" / "my_agents"
    _write(
        wrong_import / "__init__.py",
        "from my_agents.core.types import AppError\n",
    )

    bad_findings = check_thin_entrypoint(bad_src)
    assert len(bad_findings) == 2
    assert {f.rule for f in bad_findings} == {"PL014"}
    assert check_thin_entrypoint(good_src) == []
    assert len(check_thin_entrypoint(wrong_import)) == 1


def test_pl015_flags_global_environ_mutation_and_allows_reads(
    tmp_path: Path,
) -> None:
    src = _package(tmp_path)
    _write(
        src / "bad.py",
        """
        import os
        from os import environ
        def mutate() -> None:
            os.environ['FOO'] = '1'
            environ.setdefault('BAR', '2')
            os.putenv('BAZ', '3')
            del os.environ['FOO']
        """,
    )
    _write(
        src / "good.py",
        """
        from os import environ
        def read_value(name: str) -> str | None:
            return environ.get(name)
        """,
    )

    findings = check_no_global_environ_mutation(src)

    assert len(findings) == 4
    assert {f.rule for f in findings} == {"PL015"}
    assert all(f.path.name == "bad.py" for f in findings)


def test_pl016_flags_mutable_or_abstract_collections_in_frozen_dataclass(
    tmp_path: Path,
) -> None:
    src = _package(tmp_path)
    _write(
        src / "dtos.py",
        """
        from collections.abc import Callable, Sequence
        from dataclasses import dataclass
        from typing import ClassVar
        @dataclass(frozen=True, slots=True)
        class BadDto:
            items: list[str]
            history: Sequence[str]
            mapping: dict[str, int] | None
        @dataclass(frozen=True, slots=True)
        class GoodDto:
            items: tuple[str, ...]
            tags: frozenset[str]
            callback: Callable[[list[str]], None]
            cache: ClassVar[dict[str, str]] = {}
        """,
    )

    findings = check_immutable_frozen_dataclass_fields(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL016"}
    messages = " ".join(f.message for f in findings)
    assert "items" in messages
    assert "mapping" in messages
    assert "history" not in messages
    assert "callback" not in messages
    assert "cache" not in messages


def test_pl018_flags_cross_layer_monkeypatch_and_allows_own_module(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    _write(
        tests_dir / "test_composition.py",
        """
        import pydantic_ai
        import pytest
        from my_agents import composition
        from my_agents.infrastructure import runner as runner_mod
        def test_sample(monkeypatch: pytest.MonkeyPatch) -> None:
            monkeypatch.setattr('my_agents.composition.Runner', object())
            monkeypatch.setattr(composition, 'Runner', object())
            monkeypatch.setattr(pydantic_ai, 'BANNER_ENABLED', True)
            monkeypatch.setattr('my_agents.infrastructure.runner.Client', object())
            monkeypatch.setattr(runner_mod, 'Client', object())
        """,
    )

    findings = check_no_cross_layer_internal_monkeypatch(tests_dir)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL018"}
    messages = " ".join(f.message for f in findings)
    assert "my_agents.infrastructure.runner.Client" in messages
    assert "my_agents.infrastructure.runner" in messages


def test_pl019_flags_exception_subclasses_in_src(tmp_path: Path) -> None:
    src = _package(tmp_path)
    _write(
        src / "errors.py",
        """
        from dataclasses import dataclass
        class BadDomainError(Exception):
            pass
        class BadDerivedError(BadDomainError):
            pass
        @dataclass(frozen=True, slots=True)
        class GoodDomainError:
            message: str
        """,
    )

    findings = check_no_exception_result_errors(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL019"}
    messages = " ".join(f.message for f in findings)
    assert "BadDomainError" in messages
    assert "BadDerivedError" in messages
    assert "GoodDomainError" not in messages


def test_pl020_restricts_logging_to_the_shell_logger_module(
    tmp_path: Path,
) -> None:
    src = _package(tmp_path)
    _write(src / "core" / "bad_core.py", "import logging\n")
    _write(src / "shell" / "bad_shell.py", "import logging\n")
    _write(
        src / "shell" / "logger.py",
        """
        from logging import Formatter
        from logging.handlers import RotatingFileHandler
        formatter = Formatter('%(message)s')
        handler.handle(record)
        """,
    )

    findings = check_no_direct_logging(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL020"}
    assert {f.path.name for f in findings} == {"bad_core.py", "bad_shell.py"}
    messages = " ".join(f.message for f in findings)
    assert "'shell/logger.py'" in messages


def test_pl002_requires_expected_type_for_typed_error(tmp_path: Path) -> None:
    src = _package(tmp_path)
    tests_dir = tmp_path / "tests"
    _write(
        src / "usecases" / "errors.py",
        """
        from dataclasses import dataclass
        from enum import Enum, auto
        class UseCaseErrorType(Enum):
            INVALID = auto()
        @dataclass(frozen=True, slots=True)
        class UseCaseError:
            type: UseCaseErrorType
            message: str
        @dataclass(frozen=True, slots=True)
        class SettingsError:
            message: str
        """,
    )
    _write(
        tests_dir / "test_sample.py",
        """
        def test_bad_missing_expected_type() -> None:
            error = UseCaseError(type=UseCaseErrorType.INVALID, message='bad')
            assert isinstance(error, UseCaseError)
            assert_err(object(), message_contains='bad')
        def test_good_with_expected_type() -> None:
            assert_err(
                object(),
                expected_type=UseCaseErrorType.INVALID,
                message_contains='bad',
            )
        def test_allows_untyped_app_error() -> None:
            assert_err(object(), message_contains='missing')
        """,
    )

    findings = check_assert_error_message(tests_dir)

    assert len(findings) == 1
    assert findings[0].rule == "PL002"
    assert "test_bad_missing_expected_type" in findings[0].message
    assert "expected_type" in findings[0].hint


def test_pl022_requires_tool_and_parameter_descriptions(tmp_path: Path) -> None:
    src = _package(tmp_path)
    _write(
        src / "infrastructure" / "tools.py",
        """
        from typing import Annotated
        from pydantic import Field
        from pydantic_ai import RunContext, Tool
        def described(value: Annotated[str, Field(description='ok')]) -> str:
            return value
        def bad_param(bad_value: str) -> str:
            return bad_value
        def empty_field(
            empty_value: Annotated[str, Field(description='   ')],
        ) -> str:
            return empty_value
        def build() -> object:
            def nested(nested_value: str) -> str:
                return nested_value
            return Tool(nested, description='nested tool')
        async def valid(
            ctx: RunContext[None],
            value: Annotated[str, Field(description='ok')],
        ) -> str:
            return value
        tool_missing = Tool(described)
        tool_empty = Tool(described, description='   ')
        tool_bad_param = Tool(bad_param, description='does things')
        tool_empty_field = Tool(empty_field, description='does things')
        tool_valid = Tool(valid, description='valid')
        tool_none = Tool(described, description=None)
        """,
    )

    findings = check_tool_descriptions(src)

    assert len(findings) == 6
    assert {finding.rule for finding in findings} == {"PL022"}
    messages = " ".join(finding.message for finding in findings)
    assert "description=" in messages
    assert "bad_value" in messages
    assert "empty_value" in messages
    assert "nested_value" in messages
    assert "valid" not in messages


def test_pl023_regression_private_type_in_public_api(tmp_path: Path) -> None:
    # 2026-11-22 | thread mcp:launch:1a4040d4c0a04b9a800ad4bbe24f3bfa7c6c26d232c6018cca491c9cb0b465e0 | SHAs 56de693, 83ff9e4
    sample = _write(
        tmp_path / "sample.py",
        """
        class JsonlFileLogger:
            def __init__(self, handler: _PrivateRotatingFileHandler) -> None: ...
        class PydanticAIEpisodeSummarizer:
            def __init__(
                self, model_or_agent: Model | Agent[None, _EpisodeSummaryOutput],
            ) -> None:
                self._agent: Agent[None, _EpisodeSummaryOutput] = model_or_agent
        class Public(_Base[models._Config]):
            attribute: Annotated[_Value | _Value, _META]
            _private: _Value
            def _build(self, value: _Value) -> _Value: ...
            def __str__(self: _Receiver) -> str: ...
            @classmethod
            def create(cls: _Receiver) -> str: ...
            class Nested:
                async def get(self) -> _namespace._NestedValue: ...
                type LocalAlias = _Value
        class _Internal:
            attribute: _Value
        async def fetch() -> _Result:
            def local() -> _Result: ...
            return local()
        type PublicAlias = dict[str, _Value]
        type _PrivateAlias = _Value
        public_value: list[_Value]
        _private_value: _Value
        def parameters(self: _Pos, /, regular: _Regular, *items: _Item,
                       flag: _Flag, **options: _Option): ...
        def valid(value: Annotated[str, _META], raw: Literal['_raw'],
                  qualified: typing.Annotated[str, Field(_META)],
                  sentinel: typing.Literal[constants._RAW],
                  dunder: __Public__) -> Public: ...
        """,
    )
    _write(tmp_path / "_helper.py", "def hidden() -> _Value: ...\n")
    _write(tmp_path / "_helpers" / "api.py", "def hidden() -> _Value: ...\n")
    init = _write(tmp_path / "__init__.py", "exported: _Value\n")
    main = _write(tmp_path / "__main__.py", "def main() -> _Value: ...\n")

    findings = check_no_private_type_in_public_api(tmp_path)

    assert len(findings) == 15
    assert {finding.rule for finding in findings} == {"PL023"}
    assert {(finding.path, finding.line, finding.col) for finding in findings} >= {
        (sample, 2, 24),
        (sample, 5, 15),
        (sample, 16, 9),
        (init, 1, 1),
        (main, 1, 1),
    }
    assert {finding.message for finding in findings} == {
        f"Public API '{symbol}' exposes private type(s) {names}."
        for symbol, names in (
            ("JsonlFileLogger.__init__(handler)", "_PrivateRotatingFileHandler"),
            (
                "PydanticAIEpisodeSummarizer.__init__(model_or_agent)",
                "_EpisodeSummaryOutput",
            ),
            ("Public base", "_Base, _Config"),
            ("Public.attribute", "_Value"),
            ("Public.Nested.get() return", "_NestedValue, _namespace"),
            ("fetch() return", "_Result"),
            ("PublicAlias", "_Value"),
            ("public_value", "_Value"),
            ("parameters(self)", "_Pos"),
            ("parameters(regular)", "_Regular"),
            ("parameters(items)", "_Item"),
            ("parameters(flag)", "_Flag"),
            ("parameters(options)", "_Option"),
            ("exported", "_Value"),
            ("main() return", "_Value"),
        )
    }


def test_pl002_requires_kind_for_app_error(tmp_path: Path) -> None:
    src = _package(tmp_path)
    tests_dir = tmp_path / "tests"
    _write(
        src / "core" / "types.py",
        """
        from dataclasses import dataclass
        from enum import Enum, auto
        class AppErrorKind(Enum):
            INVALID = auto()
        @dataclass(frozen=True)
        class AppError:
            kind: AppErrorKind
            message: str
        """,
    )
    _write(
        tests_dir / "test_sample.py",
        """
        def test_bad_missing_kind() -> None:
            result = object()
            assert isinstance(result, AppError)
            assert_err(result, message_contains='bad')
        def test_good_kind() -> None:
            assert_err(
                object(),
                kind=AppErrorKind.INVALID,
                message_contains='bad',
            )
        """,
    )

    findings = check_assert_error_message(tests_dir, src_dir=src)

    assert len(findings) == 1
    assert findings[0].rule == "PL002"
    assert "test_bad_missing_kind" in findings[0].message
