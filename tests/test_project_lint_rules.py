from pathlib import Path

from tools.project_lint import (
    check_adapter_port_surface,
    check_assert_error_message,
    check_exhaustive_branch,
    check_immutable_frozen_dataclass_fields,
    check_layer_boundaries,
    check_no_adapter_reexport_at_layer_root,
    check_no_cast_in_src,
    check_no_cross_layer_internal_monkeypatch,
    check_no_direct_logging,
    check_no_docstrings,
    check_no_exception_result_errors,
    check_no_global_environ_mutation,
    check_no_inline_suppressions,
    check_no_object_in_contravariant_type_args,
    check_no_plain_str_secrets,
    check_no_private_dataclass_init_fields,
    check_no_shallow_tests,
    check_strict_coverage_config,
    check_strict_pyright_config,
    check_thin_entrypoint,
    check_tool_descriptions,
    check_usecase_protocol_ownership,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_pl001_flags_plain_str_secret_and_allows_vo_and_non_secrets(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "contracts" / "secret.py",
        (
            "from dataclasses import dataclass, field\n"
            "\n"
            "class SecretStr:\n"
            "    __slots__ = ('_value',)\n"
            "    def __init__(self, value: str, /) -> None:\n"
            "        self._value = value\n"
            "    def __repr__(self) -> str:\n"
            "        return 'SecretStr(***)'\n"
            "    def __str__(self) -> str:\n"
            "        return '***'\n"
            "    def __eq__(self, other: object) -> bool:\n"
            "        return isinstance(other, SecretStr) and self._value == other._value\n"
            "    def __hash__(self) -> int:\n"
            "        return hash(self._value)\n"
            "    def get_secret_value(self) -> str:\n"
            "        return self._value\n"
            "\n"
            "@dataclass(frozen=True)\n"
            "class DataclassSecret:\n"
            "    value: str = field(repr=False)\n"
            "    def __repr__(self) -> str:\n"
            "        return '***'\n"
            "    def __str__(self) -> str:\n"
            "        return '***'\n"
            "    def __eq__(self, other: object) -> bool:\n"
            "        return False\n"
            "    def __hash__(self) -> int:\n"
            "        return 0\n"
            "\n"
            "class IncompleteSecret:\n"
            "    def __init__(self, value: str, /) -> None:\n"
            "        self.value = value\n"
        ),
    )
    _write(
        src / "infrastructure" / "bad.py",
        (
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class BadConfig:\n"
            "    api_key: str\n"
            "    auth_token: str | None\n"
            "    max_tokens: int\n"
            "    token_count: str\n"
            "    key_name: str\n"
        ),
    )
    _write(
        src / "infrastructure" / "good.py",
        (
            "from dataclasses import dataclass\n"
            "from my_agents.contracts.secret import SecretStr\n"
            "@dataclass(frozen=True)\n"
            "class GoodConfig:\n"
            "    api_key: SecretStr\n"
            "    auth_token: SecretStr | None\n"
        ),
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
        (
            "def test_bad() -> None:\n"
            "    error = object()\n"
            "    assert error.type == 1\n"
            "\n"
            "def test_good_direct() -> None:\n"
            "    error = object()\n"
            "    assert error.type == 1\n"
            "    assert 'boom' in error.message\n"
            "\n"
            "def test_good_helper() -> None:\n"
            "    assert_err(object(), expected_type=1, message_contains='boom')\n"
        ),
    )

    findings = check_assert_error_message(tests_dir)

    assert len(findings) == 1
    assert findings[0].rule == "PL002"
    assert "test_bad" in findings[0].message


def test_pl003_flags_non_exhaustive_if_else_on_literal_and_enum(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "contracts" / "types.py",
        (
            "from dataclasses import dataclass\n"
            "from enum import Enum, auto\n"
            "from typing import Literal\n"
            "class Mode(Enum):\n"
            "    FAST = auto()\n"
            "    SLOW = auto()\n"
            "@dataclass(frozen=True)\n"
            "class Msg:\n"
            "    role: Literal['user', 'assistant']\n"
        ),
    )
    _write(
        src / "infrastructure" / "sample.py",
        (
            "from typing import assert_never\n"
            "def bad_literal(msg: object) -> int:\n"
            "    if msg.role == 'user':\n"
            "        return 1\n"
            "    else:\n"
            "        return 2\n"
            "\n"
            "def bad_enum(mode: object) -> int:\n"
            "    if mode is Mode.FAST:\n"
            "        return 1\n"
            "    else:\n"
            "        return 2\n"
            "\n"
            "def good_plain_str(prompt: str) -> int:\n"
            "    if prompt == 'quit':\n"
            "        return 0\n"
            "    else:\n"
            "        return 1\n"
            "\n"
            "def good_match(msg: object) -> int:\n"
            "    match msg.role:\n"
            "        case 'user':\n"
            "            return 1\n"
            "        case 'assistant':\n"
            "            return 2\n"
            "        case _:\n"
            "            assert_never(msg.role)\n"
        ),
    )

    findings = check_exhaustive_branch(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL003"}


def test_pl004_enforces_clean_architecture_layer_boundaries(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "contracts" / "bad.py",
        (
            "import pydantic_ai\n"
            "from my_agents.infrastructure.settings import AppSettings\n"
        ),
    )
    _write(
        src / "domain" / "bad.py",
        "from my_agents.contracts.messages import ChatMessage\n",
    )
    _write(
        src / "infrastructure" / "ok.py",
        ("import pydantic_ai\nfrom my_agents.contracts.messages import ChatMessage\n"),
    )

    findings = check_layer_boundaries(src)

    assert len(findings) == 3
    assert {f.rule for f in findings} == {"PL004"}
    assert any(
        finding.path == src / "domain" / "bad.py"
        and "must not import from 'contracts'" in finding.message
        for finding in findings
    )


def test_pl005_flags_weakened_pyright_config(tmp_path: Path) -> None:
    bad_toml = _write(
        tmp_path / "bad.toml",
        (
            "[tool.pyright]\n"
            'typeCheckingMode = "strict"\n'
            "reportUnknownVariableType = false\n"
            "reportPrivateUsage = false\n"
        ),
    )
    good_toml = _write(
        tmp_path / "good.toml",
        ('[tool.pyright]\ntypeCheckingMode = "strict"\n'),
    )

    bad_findings = check_strict_pyright_config(bad_toml)
    good_findings = check_strict_pyright_config(good_toml)

    assert len(bad_findings) == 2
    assert {f.rule for f in bad_findings} == {"PL005"}
    assert good_findings == []


def test_pl006_flags_docstrings_and_allows_runtime_decorator_exceptions(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "bad.py",
        (
            '"""Module docstring."""\n'
            "\n"
            "class Sample:\n"
            '    """Class docstring."""\n'
            "\n"
            "    value: int\n"
            '    """Attribute docstring."""\n'
            "\n"
            "    def method(self) -> str:\n"
            '        """Method docstring."""\n'
            '        return "ok"\n'
            "\n"
            "async def fetch() -> str:\n"
            '    """Async docstring."""\n'
            '    return "ok"\n'
        ),
    )
    _write(
        src / "good.py",
        (
            "from abc import ABC, abstractmethod\n"
            "\n"
            "class Port(ABC):\n"
            "    @abstractmethod\n"
            "    async def run(self) -> str: ...\n"
            "\n"
            "def greet() -> str:\n"
            '    message = "hello"\n'
            "    return message\n"
        ),
    )
    _write(
        src / "runtime_tool.py",
        (
            "@agent.tool\n"
            "def lookup_weather(city: str) -> str:\n"
            '    """Look up current weather for a city."""\n'
            "    return city\n"
            "\n"
            "@other.decorator\n"
            "def regular_helper() -> str:\n"
            '    """Unallowed docstring."""\n'
            '    return "ok"\n'
        ),
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
        (
            "[tool.coverage.run]\n"
            "branch = false\n"
            'omit = ["src/my_agents/__init__.py", "src/my_agents/infrastructure/a.py"]\n'
            "[tool.coverage.report]\n"
            "fail_under = 80\n"
            'exclude_also = ["if TYPE_CHECKING:", "raise NotImplementedError"]\n'
        ),
    )
    good_toml = _write(
        tmp_path / "good.toml",
        (
            "[tool.coverage.run]\n"
            "branch = true\n"
            'omit = ["src/my_agents/__init__.py"]\n'
            "[tool.coverage.report]\n"
            "fail_under = 100\n"
            'exclude_also = ["if TYPE_CHECKING:", "assert_never\\\\("]\n'
        ),
    )

    bad_findings = check_strict_coverage_config(bad_toml)
    good_findings = check_strict_coverage_config(good_toml)

    assert len(bad_findings) == 4
    assert {f.rule for f in bad_findings} == {"PL007"}
    assert good_findings == []


def test_pl008_flags_inline_suppressions_and_ignores_strings(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "bad.py",
        (
            "a = 1  # type: ignore[assignment]\n"
            "b = 2  # pyright: ignore[reportArgumentType]\n"
            "c = 3  # noqa: TID251\n"
            "d = 4  # pragma: no cover\n"
        ),
    )
    _write(
        src / "good.py",
        (
            'text = "# noqa: TID251 # type: ignore # pragma: no cover"\n'
            "value = 1  # regular why note\n"
        ),
    )

    findings = check_no_inline_suppressions((src,))

    assert len(findings) == 4
    assert {f.rule for f in findings} == {"PL008"}
    assert all(f.path.name == "bad.py" for f in findings)


def test_pl009_flags_typing_cast_in_src(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "bad_import.py",
        (
            "from typing import cast\ndef f(x: object) -> int:\n    return cast(int, x)\n"
        ),
    )
    _write(
        src / "bad_attr.py",
        ("import typing as t\ndef f(x: object) -> int:\n    return t.cast(int, x)\n"),
    )
    _write(
        src / "good.py",
        (
            "def f(x: object) -> int:\n"
            "    if isinstance(x, int):\n"
            "        return x\n"
            "    return 0\n"
        ),
    )

    findings = check_no_cast_in_src(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL009"}
    assert {f.path.name for f in findings} == {"bad_import.py", "bad_attr.py"}


def test_pl010_flags_shallow_or_assertionless_tests(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    _write(
        tests_dir / "test_sample.py",
        (
            "def test_only_isinstance() -> None:\n"
            "    value = object()\n"
            "    assert isinstance(value, object)\n"
            "\n"
            "def test_only_not_none() -> None:\n"
            "    value = object()\n"
            "    assert value is not None\n"
            "\n"
            "def test_no_assert() -> None:\n"
            "    _ = 1 + 1\n"
            "\n"
            "def test_good_equality() -> None:\n"
            "    value = 1 + 1\n"
            "    assert isinstance(value, int)\n"
            "    assert value == 2\n"
            "\n"
            "def test_good_raises() -> None:\n"
            "    with pytest.raises(ValueError):\n"
            "        int('bad')\n"
            "\n"
            "def test_good_helper() -> None:\n"
            "    assert_err(object(), message_contains='boom')\n"
        ),
    )

    findings = check_no_shallow_tests(tests_dir)

    assert len(findings) == 3
    assert {f.rule for f in findings} == {"PL010"}
    messages = " ".join(f.message for f in findings)
    assert "test_only_isinstance" in messages
    assert "test_only_not_none" in messages
    assert "test_no_assert" in messages


def test_pl011_flags_private_dataclass_init_fields(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "sample.py",
        (
            "from dataclasses import dataclass, field\n"
            "from typing import ClassVar\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class BadSecret:\n"
            "    _value: str\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class GoodPublicField:\n"
            "    value: str = field(repr=False)\n"
            "    _cache: int = field(default=0, init=False)\n"
            "    _tag: ClassVar[str] = 'ok'\n"
            "\n"
            "@dataclass(frozen=True, slots=True, init=False)\n"
            "class GoodExplicitInit:\n"
            "    _value: str\n"
            "    def __init__(self, value: str, /) -> None:\n"
            "        object.__setattr__(self, '_value', value)\n"
        ),
    )

    findings = check_no_private_dataclass_init_fields(src)

    assert len(findings) == 1
    assert findings[0].rule == "PL011"
    assert "BadSecret" in findings[0].message
    assert "_value" in findings[0].message


def test_pl012_flags_object_in_contravariant_agent_type_arg(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "sample.py",
        (
            "def bad(agent: Agent[object, str]) -> Agent[object, str]:\n"
            "    return agent\n"
            "\n"
            "def good(agent: Agent[None, str], data: dict[str, object]) -> None:\n"
            "    _ = (agent, data)\n"
        ),
    )

    findings = check_no_object_in_contravariant_type_args(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL012"}


def test_pl013_flags_adapter_public_members_outside_port_contract(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "contracts" / "ports" / "runner.py",
        (
            "from abc import ABC, abstractmethod\n"
            "class IRunner(ABC):\n"
            "    @abstractmethod\n"
            "    async def run(self) -> str: ...\n"
        ),
    )
    _write(
        src / "infrastructure" / "runner.py",
        (
            "from my_agents.contracts.ports.runner import IRunner\n"
            "class ConcreteRunner(IRunner):\n"
            "    def __init__(self) -> None:\n"
            "        self._inner = object()\n"
            "    @classmethod\n"
            "    def from_settings(cls) -> ConcreteRunner:\n"
            "        return cls()\n"
            "    @property\n"
            "    def model(self) -> object:\n"
            "        return self._inner\n"
            "    def extra_helper(self) -> None:\n"
            "        return None\n"
            "    def _private_helper(self) -> None:\n"
            "        return None\n"
            "    async def run(self) -> str:\n"
            "        return 'ok'\n"
        ),
    )

    findings = check_adapter_port_surface(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL013"}
    messages = " ".join(f.message for f in findings)
    assert "model" in messages
    assert "extra_helper" in messages
    assert "from_settings" not in messages
    assert "_private_helper" not in messages


def test_pl002_requires_display_message_check_for_displayable_error(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    tests_dir = tmp_path / "tests"
    _write(
        src / "usecases" / "errors.py",
        (
            "from dataclasses import dataclass\n"
            "from enum import Enum, auto\n"
            "\n"
            "class UseCaseErrorType(Enum):\n"
            "    INVALID = auto()\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class UseCaseError:\n"
            "    type: UseCaseErrorType\n"
            "    message: str\n"
            "    display_message: str\n"
        ),
    )
    _write(
        tests_dir / "test_usecase.py",
        (
            "def test_bad_missing_display_check() -> None:\n"
            "    assert_err(object(), expected_type=UseCaseErrorType.INVALID, message_contains='bad')\n"
            "\n"
            "def test_good_helper_display_check() -> None:\n"
            "    assert_err(\n"
            "        object(),\n"
            "        expected_type=UseCaseErrorType.INVALID,\n"
            "        message_contains='bad',\n"
            "        display_message_contains='user msg',\n"
            "    )\n"
            "\n"
            "def test_good_attr_display_check() -> None:\n"
            "    error = assert_err(\n"
            "        object(),\n"
            "        expected_type=UseCaseErrorType.INVALID,\n"
            "        message_contains='bad',\n"
            "    )\n"
            "    assert error.display_message == 'user msg'\n"
            "\n"
            "def test_ignored_fake_instantiation_without_assert() -> None:\n"
            "    fake_err = UseCaseError(\n"
            "        type=UseCaseErrorType.INVALID,\n"
            "        message='internal',\n"
            "        display_message='user msg',\n"
            "    )\n"
            "    assert fake_err.display_message == 'user msg'\n"
        ),
    )

    findings = check_assert_error_message(tests_dir)

    assert len(findings) == 1
    assert findings[0].rule == "PL002"
    assert "test_bad_missing_display_check" in findings[0].message


def test_pl014_enforces_thin_entrypoint(tmp_path: Path) -> None:
    bad_src = tmp_path / "bad" / "src" / "my_agents"
    _write(
        bad_src / "__init__.py",
        (
            "import os\n"
            "from .composition import run_cli\n"
            "\n"
            "def main() -> int:\n"
            "    if os.environ.get('DEBUG'):\n"
            "        return 1\n"
            "    return run_cli()\n"
        ),
    )
    good_src = tmp_path / "good" / "src" / "my_agents"
    _write(
        good_src / "__init__.py",
        (
            "from my_agents.composition import run_cli\n"
            "\n"
            "def main() -> int:\n"
            "    return run_cli()\n"
        ),
    )
    good_rel_src = tmp_path / "good_rel" / "src" / "my_agents"
    _write(
        good_rel_src / "__init__.py",
        (
            "from .composition import run_cli\n"
            "\n"
            "def main() -> int:\n"
            "    return run_cli()\n"
        ),
    )

    bad_findings = check_thin_entrypoint(bad_src)
    assert len(bad_findings) == 2
    assert {f.rule for f in bad_findings} == {"PL014"}
    assert check_thin_entrypoint(good_src) == []
    assert check_thin_entrypoint(good_rel_src) == []


def test_pl015_flags_global_environ_mutation_and_allows_reads(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "bad.py",
        (
            "import os\n"
            "from os import environ\n"
            "\n"
            "def mutate() -> None:\n"
            "    os.environ['FOO'] = '1'\n"
            "    environ.setdefault('BAR', '2')\n"
            "    os.putenv('BAZ', '3')\n"
            "    del os.environ['FOO']\n"
        ),
    )
    _write(
        src / "good.py",
        (
            "from os import environ\n"
            "\n"
            "def read_value(name: str) -> str | None:\n"
            "    return environ.get(name)\n"
        ),
    )

    findings = check_no_global_environ_mutation(src)

    assert len(findings) == 4
    assert {f.rule for f in findings} == {"PL015"}
    assert all(f.path.name == "bad.py" for f in findings)


def test_pl016_flags_mutable_or_abstract_collections_in_frozen_dataclass(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "dtos.py",
        (
            "from collections.abc import Callable, Sequence\n"
            "from dataclasses import dataclass\n"
            "from typing import ClassVar\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class BadDto:\n"
            "    items: list[str]\n"
            "    history: Sequence[str]\n"
            "    mapping: dict[str, int] | None\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class GoodDto:\n"
            "    items: tuple[str, ...]\n"
            "    tags: frozenset[str]\n"
            "    callback: Callable[[list[str]], None]\n"
            "    cache: ClassVar[dict[str, str]] = {}\n"
        ),
    )

    findings = check_immutable_frozen_dataclass_fields(src)

    assert len(findings) == 3
    assert {f.rule for f in findings} == {"PL016"}
    messages = " ".join(f.message for f in findings)
    assert "items" in messages
    assert "history" in messages
    assert "mapping" in messages
    assert "callback" not in messages
    assert "cache" not in messages


def test_pl017_flags_subpackage_reexports_at_layer_root(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(src / "presentation" / "cli" / "repl.py", "class CliRepl: ...\n")
    _write(
        src / "presentation" / "__init__.py",
        (
            "from .cli import CliRepl\n"
            "from my_agents.presentation.cli.repl import CliRepl as Repl\n"
        ),
    )
    _write(src / "usecases" / "send.py", "class SendHandler: ...\n")
    _write(
        src / "usecases" / "__init__.py",
        "from my_agents.usecases.send import SendHandler\n",
    )

    findings = check_no_adapter_reexport_at_layer_root(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL017"}
    assert all(f.path.parent.name == "presentation" for f in findings)


def test_pl018_flags_cross_layer_monkeypatch_and_allows_own_module(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    _write(
        tests_dir / "test_composition.py",
        (
            "import pydantic_ai\n"
            "import pytest\n"
            "from my_agents import composition\n"
            "from my_agents.infrastructure import runner as runner_mod\n"
            "\n"
            "def test_sample(monkeypatch: pytest.MonkeyPatch) -> None:\n"
            "    monkeypatch.setattr('my_agents.composition.Runner', object())\n"
            "    monkeypatch.setattr(composition, 'Runner', object())\n"
            "    monkeypatch.setattr(pydantic_ai, 'BANNER_ENABLED', True)\n"
            "    monkeypatch.setattr('my_agents.infrastructure.runner.Client', object())\n"
            "    monkeypatch.setattr(runner_mod, 'Client', object())\n"
        ),
    )

    findings = check_no_cross_layer_internal_monkeypatch(tests_dir)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL018"}
    messages = " ".join(f.message for f in findings)
    assert "my_agents.infrastructure.runner.Client" in messages
    assert "my_agents.infrastructure.runner" in messages


def test_pl019_flags_exception_subclasses_in_src(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "errors.py",
        (
            "from dataclasses import dataclass\n"
            "\n"
            "class BadDomainError(Exception):\n"
            "    pass\n"
            "\n"
            "class BadDerivedError(BadDomainError):\n"
            "    pass\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class GoodDomainError:\n"
            "    message: str\n"
        ),
    )

    findings = check_no_exception_result_errors(src)

    assert len(findings) == 2
    assert {f.rule for f in findings} == {"PL019"}
    messages = " ".join(f.message for f in findings)
    assert "BadDomainError" in messages
    assert "BadDerivedError" in messages
    assert "GoodDomainError" not in messages


def test_pl020_restricts_direct_logging_imports_and_global_apis(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "usecases" / "bad_usecase.py",
        ("import logging\nfrom logging import Formatter\n"),
    )
    _write(
        src / "composition.py",
        "import logging.handlers\n",
    )
    _write(
        src / "infrastructure" / "bad_infra.py",
        (
            "import logging\n"
            "import logging as lg\n"
            "import logging.handlers\n"
            "import logging.config\n"
            "from logging import config, getLogger\n"
            "from logging.config import dictConfig\n"
            "from logging import *\n"
            "from logging.handlers import *\n"
            "logger = logging.getLogger(__name__)\n"
            "lg.basicConfig()\n"
            "logging.error('oops')\n"
            "logging.config.dictConfig({})\n"
            "handler.emit(record)\n"
        ),
    )
    _write(
        src / "infrastructure" / "good_infra.py",
        (
            "from logging import ERROR, Formatter, LogRecord\n"
            "from logging.handlers import RotatingFileHandler\n"
            "import logging.handlers as lh\n"
            "class CustomHandler(RotatingFileHandler):\n"
            "    def emit(self, record: LogRecord) -> None:\n"
            "        super().emit(record)\n"
            "formatter = Formatter('%(message)s')\n"
            "handler.handle(record)\n"
        ),
    )
    _write(
        src / "infrastructure" / "non_logging_infra.py",
        "bus.emit('event')\n",
    )

    findings = check_no_direct_logging(src)

    assert len(findings) == 14
    assert {f.rule for f in findings} == {"PL020"}
    assert all(
        f.path.name not in {"good_infra.py", "non_logging_infra.py"} for f in findings
    )
    messages = " ".join(f.message for f in findings)
    assert "logging.getLogger" in messages
    assert "lg.basicConfig" in messages
    assert "logging.error" in messages
    assert "logging.config" in messages
    assert "wildcard-import" in messages
    assert ".emit()" in messages
    assert "logging.handlers" in messages


def test_pl002_requires_expected_type_for_typed_error(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    tests_dir = tmp_path / "tests"
    _write(
        src / "usecases" / "errors.py",
        (
            "from dataclasses import dataclass\n"
            "from enum import Enum, auto\n"
            "\n"
            "class UseCaseErrorType(Enum):\n"
            "    INVALID = auto()\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class UseCaseError:\n"
            "    type: UseCaseErrorType\n"
            "    message: str\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class SettingsError:\n"
            "    message: str\n"
        ),
    )
    _write(
        tests_dir / "test_sample.py",
        (
            "def test_bad_missing_expected_type() -> None:\n"
            "    error = UseCaseError(type=UseCaseErrorType.INVALID, message='bad')\n"
            "    assert isinstance(error, UseCaseError)\n"
            "    assert_err(object(), message_contains='bad')\n"
            "\n"
            "def test_good_with_expected_type() -> None:\n"
            "    assert_err(\n"
            "        object(),\n"
            "        expected_type=UseCaseErrorType.INVALID,\n"
            "        message_contains='bad',\n"
            "    )\n"
            "\n"
            "def test_allows_untyped_app_error() -> None:\n"
            "    assert_err(object(), message_contains='missing')\n"
        ),
    )

    findings = check_assert_error_message(tests_dir)

    assert len(findings) == 1
    assert findings[0].rule == "PL002"
    assert "test_bad_missing_expected_type" in findings[0].message
    assert "expected_type" in findings[0].hint


def test_pl021_requires_usecase_protocol_to_match_module_name(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "usecases" / "send_message.py",
        (
            "from typing import Protocol\n"
            "\n"
            "class SendMessageUseCase(Protocol):\n"
            "    def execute(self) -> None: ...\n"
            "\n"
            "class _LocalUseCase(Protocol):\n"
            "    def execute(self) -> None: ...\n"
            "\n"
            "class SendMessageHandler(Protocol):\n"
            "    def execute(self) -> None: ...\n"
        ),
    )
    _write(
        src / "usecases" / "get_core_profile.py",
        (
            "from typing import Protocol\n"
            "\n"
            "class GetCoreProfileUseCase(Protocol):\n"
            "    def execute(self) -> None: ...\n"
            "\n"
            "class SendMessageUseCase(Protocol):\n"
            "    def execute(self) -> None: ...\n"
        ),
    )

    findings = check_usecase_protocol_ownership(src)

    assert len(findings) == 1
    assert findings[0].rule == "PL021"
    assert "SendMessageUseCase" in findings[0].message
    assert "get_core_profile" in findings[0].message
    assert findings[0].hint


def test_pl022_requires_tool_and_parameter_descriptions(tmp_path: Path) -> None:
    src = tmp_path / "src" / "my_agents"
    _write(
        src / "infrastructure" / "tools.py",
        (
            "from typing import Annotated\n"
            "from pydantic import Field\n"
            "from pydantic_ai import RunContext, Tool\n"
            "\n"
            "def described(value: Annotated[str, Field(description='ok')]) -> str:\n"
            "    return value\n"
            "\n"
            "def bad_param(bad_value: str) -> str:\n"
            "    return bad_value\n"
            "\n"
            "def empty_field(\n"
            "    empty_value: Annotated[str, Field(description='   ')],\n"
            ") -> str:\n"
            "    return empty_value\n"
            "\n"
            "def build() -> object:\n"
            "    def nested(nested_value: str) -> str:\n"
            "        return nested_value\n"
            "    return Tool(nested, description='nested tool')\n"
            "\n"
            "async def valid(\n"
            "    ctx: RunContext[None],\n"
            "    value: Annotated[str, Field(description='ok')],\n"
            ") -> str:\n"
            "    return value\n"
            "\n"
            "tool_missing = Tool(described)\n"
            "tool_empty = Tool(described, description='   ')\n"
            "tool_bad_param = Tool(bad_param, description='does things')\n"
            "tool_empty_field = Tool(empty_field, description='does things')\n"
            "tool_valid = Tool(valid, description='valid')\n"
            "tool_none = Tool(described, description=None)\n"
        ),
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
