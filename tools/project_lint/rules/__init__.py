from collections.abc import Callable, Sequence
from pathlib import Path

from tools.project_lint.common import LintFinding, LintRule
from tools.project_lint.rules.pl001_secrets import check_no_plain_str_secrets
from tools.project_lint.rules.pl002_error_message_assert import (
    check_assert_error_message,
)
from tools.project_lint.rules.pl003_exhaustive_branch import check_exhaustive_branch
from tools.project_lint.rules.pl004_layer_boundaries import check_layer_boundaries
from tools.project_lint.rules.pl005_pyright_config import check_strict_pyright_config
from tools.project_lint.rules.pl006_no_docstrings import check_no_docstrings
from tools.project_lint.rules.pl007_coverage_config import check_strict_coverage_config
from tools.project_lint.rules.pl008_inline_suppressions import (
    check_no_inline_suppressions,
)
from tools.project_lint.rules.pl009_no_cast import check_no_cast_in_src
from tools.project_lint.rules.pl010_shallow_tests import check_no_shallow_tests
from tools.project_lint.rules.pl011_private_dataclass_fields import (
    check_no_private_dataclass_init_fields,
)
from tools.project_lint.rules.pl012_contravariant_object import (
    check_no_object_in_contravariant_type_args,
)
from tools.project_lint.rules.pl013_adapter_port_surface import (
    check_adapter_port_surface,
)
from tools.project_lint.rules.pl014_thin_entrypoint import check_thin_entrypoint
from tools.project_lint.rules.pl015_no_global_environ_mutation import (
    check_no_global_environ_mutation,
)
from tools.project_lint.rules.pl016_immutable_frozen_dataclass import (
    check_immutable_frozen_dataclass_fields,
)
from tools.project_lint.rules.pl017_layer_root_exports import (
    check_no_adapter_reexport_at_layer_root,
)
from tools.project_lint.rules.pl018_cross_layer_monkeypatch import (
    check_no_cross_layer_internal_monkeypatch,
)
from tools.project_lint.rules.pl019_no_exception_errors import (
    check_no_exception_result_errors,
)
from tools.project_lint.rules.pl020_no_direct_logging import check_no_direct_logging


def _src_rule(
    code: str,
    name: str,
    check: Callable[[Path], list[LintFinding]],
) -> LintRule:
    def _run(root: Path) -> list[LintFinding]:
        return check(root / "src" / "my_agents")

    return LintRule(code=code, name=name, check_root=_run)


def _tests_rule(
    code: str,
    name: str,
    check: Callable[[Path], list[LintFinding]],
) -> LintRule:
    def _run(root: Path) -> list[LintFinding]:
        return check(root / "tests")

    return LintRule(code=code, name=name, check_root=_run)


def _pyproject_rule(
    code: str,
    name: str,
    check: Callable[[Path], list[LintFinding]],
) -> LintRule:
    def _run(root: Path) -> list[LintFinding]:
        return check(root / "pyproject.toml")

    return LintRule(code=code, name=name, check_root=_run)


def _all_dirs_rule(
    code: str,
    name: str,
    check: Callable[[Sequence[Path]], list[LintFinding]],
) -> LintRule:
    def _run(root: Path) -> list[LintFinding]:
        return check((root / "src" / "my_agents", root / "tests", root / "tools"))

    return LintRule(code=code, name=name, check_root=_run)


RULES: tuple[LintRule, ...] = (
    _src_rule("PL001", "no_plain_str_secrets", check_no_plain_str_secrets),
    _tests_rule("PL002", "assert_error_message", check_assert_error_message),
    _src_rule("PL003", "exhaustive_branch", check_exhaustive_branch),
    _src_rule("PL004", "layer_boundaries", check_layer_boundaries),
    _pyproject_rule("PL005", "strict_pyright_config", check_strict_pyright_config),
    _all_dirs_rule("PL006", "no_docstrings", check_no_docstrings),
    _pyproject_rule("PL007", "strict_coverage_config", check_strict_coverage_config),
    _all_dirs_rule("PL008", "no_inline_suppressions", check_no_inline_suppressions),
    _src_rule("PL009", "no_cast_in_src", check_no_cast_in_src),
    _tests_rule("PL010", "no_shallow_tests", check_no_shallow_tests),
    _src_rule(
        "PL011",
        "no_private_dataclass_init_fields",
        check_no_private_dataclass_init_fields,
    ),
    _src_rule(
        "PL012",
        "no_object_in_contravariant_type_args",
        check_no_object_in_contravariant_type_args,
    ),
    _src_rule("PL013", "adapter_port_surface", check_adapter_port_surface),
    _src_rule("PL014", "thin_entrypoint", check_thin_entrypoint),
    _src_rule(
        "PL015",
        "no_global_environ_mutation",
        check_no_global_environ_mutation,
    ),
    _src_rule(
        "PL016",
        "immutable_frozen_dataclass_fields",
        check_immutable_frozen_dataclass_fields,
    ),
    _src_rule(
        "PL017",
        "no_adapter_reexport_at_layer_root",
        check_no_adapter_reexport_at_layer_root,
    ),
    _tests_rule(
        "PL018",
        "no_cross_layer_internal_monkeypatch",
        check_no_cross_layer_internal_monkeypatch,
    ),
    _src_rule(
        "PL019",
        "no_exception_result_errors",
        check_no_exception_result_errors,
    ),
    _src_rule(
        "PL020",
        "no_direct_logging",
        check_no_direct_logging,
    ),
)

__all__ = [
    "RULES",
    "check_adapter_port_surface",
    "check_assert_error_message",
    "check_exhaustive_branch",
    "check_immutable_frozen_dataclass_fields",
    "check_layer_boundaries",
    "check_no_adapter_reexport_at_layer_root",
    "check_no_cast_in_src",
    "check_no_cross_layer_internal_monkeypatch",
    "check_no_direct_logging",
    "check_no_docstrings",
    "check_no_exception_result_errors",
    "check_no_global_environ_mutation",
    "check_no_inline_suppressions",
    "check_no_object_in_contravariant_type_args",
    "check_no_plain_str_secrets",
    "check_no_private_dataclass_init_fields",
    "check_no_shallow_tests",
    "check_strict_coverage_config",
    "check_strict_pyright_config",
    "check_thin_entrypoint",
]
