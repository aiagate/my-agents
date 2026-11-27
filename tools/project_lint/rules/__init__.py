from collections.abc import Callable, Sequence
from pathlib import Path

from tools.project_lint.common import LintFinding, LintRule, dirs_rule, path_rule
from tools.project_lint.rules.pl001_secrets import check_no_plain_str_secrets
from tools.project_lint.rules.pl002_error_message_assert import (
    check_assert_error_message,
)
from tools.project_lint.rules.pl003_exhaustive_branch import check_exhaustive_branch
from tools.project_lint.rules.pl004_layer_boundaries import check_layer_boundaries
from tools.project_lint.rules.pl005_ty_config import check_strict_ty_config
from tools.project_lint.rules.pl006_no_docstrings import check_no_docstrings
from tools.project_lint.rules.pl007_coverage_config import check_strict_coverage_config
from tools.project_lint.rules.pl008_inline_suppressions import (
    check_no_inline_suppressions,
)
from tools.project_lint.rules.pl010_shallow_tests import check_no_shallow_tests
from tools.project_lint.rules.pl011_private_dataclass_fields import (
    check_no_private_dataclass_init_fields,
)
from tools.project_lint.rules.pl014_thin_entrypoint import check_thin_entrypoint
from tools.project_lint.rules.pl015_no_global_environ_mutation import (
    check_no_global_environ_mutation,
)
from tools.project_lint.rules.pl016_immutable_frozen_dataclass import (
    check_immutable_frozen_dataclass_fields,
)
from tools.project_lint.rules.pl018_cross_layer_monkeypatch import (
    check_no_cross_layer_internal_monkeypatch,
)
from tools.project_lint.rules.pl019_no_exception_errors import (
    check_no_exception_result_errors,
)
from tools.project_lint.rules.pl020_no_direct_logging import check_no_direct_logging
from tools.project_lint.rules.pl022_tool_descriptions import check_tool_descriptions
from tools.project_lint.rules.pl023_no_private_type_in_public_api import (
    check_no_private_type_in_public_api,
)
from tools.project_lint.rules.pl024_exception_governance import (
    check_exception_governance,
)
from tools.project_lint.rules.pl025_repo_consistency import check_repo_consistency

type _PathCheck = Callable[[Path], list[LintFinding]]
type _DirsCheck = Callable[[Sequence[Path]], list[LintFinding]]

_SRC = ("src", "my_agents")
_ALL_DIRS = (_SRC, ("tests",), ("tools",))

_PATH_RULES: tuple[tuple[str, str, tuple[str, ...], _PathCheck], ...] = (
    ("PL001", "no_plain_str_secrets", _SRC, check_no_plain_str_secrets),
    ("PL002", "assert_error_message", ("tests",), check_assert_error_message),
    ("PL003", "exhaustive_branch", _SRC, check_exhaustive_branch),
    ("PL004", "layer_boundaries", _SRC, check_layer_boundaries),
    ("PL005", "strict_ty_config", ("pyproject.toml",), check_strict_ty_config),
    (
        "PL007",
        "strict_coverage_config",
        ("pyproject.toml",),
        check_strict_coverage_config,
    ),
    ("PL010", "no_shallow_tests", ("tests",), check_no_shallow_tests),
    (
        "PL011",
        "no_private_dataclass_init_fields",
        _SRC,
        check_no_private_dataclass_init_fields,
    ),
    ("PL014", "thin_entrypoint", _SRC, check_thin_entrypoint),
    ("PL015", "no_global_environ_mutation", _SRC, check_no_global_environ_mutation),
    (
        "PL016",
        "immutable_frozen_dataclass_fields",
        _SRC,
        check_immutable_frozen_dataclass_fields,
    ),
    (
        "PL018",
        "no_cross_layer_internal_monkeypatch",
        ("tests",),
        check_no_cross_layer_internal_monkeypatch,
    ),
    ("PL019", "no_exception_result_errors", _SRC, check_no_exception_result_errors),
    ("PL020", "no_direct_logging", _SRC, check_no_direct_logging),
    (
        "PL023",
        "no_private_type_in_public_api",
        _SRC,
        check_no_private_type_in_public_api,
    ),
    ("PL024", "exception_governance", (), check_exception_governance),
    ("PL025", "repo_consistency", (), check_repo_consistency),
)

_DIRS_RULES: tuple[tuple[str, str, tuple[tuple[str, ...], ...], _DirsCheck], ...] = (
    ("PL006", "no_docstrings", _ALL_DIRS, check_no_docstrings),
    ("PL008", "no_inline_suppressions", _ALL_DIRS, check_no_inline_suppressions),
    ("PL022", "tool_descriptions", (_SRC, ("tools",)), check_tool_descriptions),
)

RULES: tuple[LintRule, ...] = tuple(
    sorted(
        (
            *(
                path_rule(code, name, parts, check)
                for code, name, parts, check in _PATH_RULES
            ),
            *(
                dirs_rule(code, name, parts, check)
                for code, name, parts, check in _DIRS_RULES
            ),
        ),
        key=lambda rule: rule.code,
    )
)
