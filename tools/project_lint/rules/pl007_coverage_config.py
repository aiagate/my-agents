import tomllib
from pathlib import Path

from tools.project_lint.common import (
    PROJECT_ROOT,
    LintFinding,
    as_table,
    find_line_number,
)

_ALLOWED_COVERAGE_OMITS = frozenset({"src/my_agents/__init__.py"})
_ALLOWED_COVERAGE_EXCLUDE_PATTERNS = frozenset(
    {
        "if TYPE_CHECKING:",
        r"case _:\s*\n\s*assert_never\(",
        r"assert_never\(",
    }
)


def _as_str_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [item for item in value if isinstance(item, str)]
    return items if len(items) == len(value) else None


def check_strict_coverage_config(
    pyproject_path: Path = PROJECT_ROOT / "pyproject.toml",
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    content = pyproject_path.read_text(encoding="utf-8")
    root_table = as_table(tomllib.loads(content))
    tool_table = as_table(root_table.get("tool")) if root_table is not None else None
    coverage_table = (
        as_table(tool_table.get("coverage")) if tool_table is not None else None
    )
    run_table = (
        as_table(coverage_table.get("run")) if coverage_table is not None else None
    )
    report_table = (
        as_table(coverage_table.get("report")) if coverage_table is not None else None
    )

    if run_table is None:
        findings.append(
            LintFinding(
                rule="PL007",
                path=pyproject_path,
                line=1,
                col=1,
                message="Missing [tool.coverage.run] configuration in pyproject.toml.",
                hint="Configure [tool.coverage.run] with branch = true.",
            )
        )
    else:
        if run_table.get("branch") is not True:
            findings.append(
                LintFinding(
                    rule="PL007",
                    path=pyproject_path,
                    line=find_line_number(content, "branch"),
                    col=1,
                    message="[tool.coverage.run] must enable 'branch = true'.",
                    hint="Set branch = true under [tool.coverage.run].",
                )
            )
        omits = _as_str_list(run_table.get("omit"))
        if omits is not None:
            for pattern in omits:
                if pattern not in _ALLOWED_COVERAGE_OMITS:
                    findings.append(
                        LintFinding(
                            rule="PL007",
                            path=pyproject_path,
                            line=find_line_number(content, pattern),
                            col=1,
                            message=(
                                f"[tool.coverage.run] omits disallowed path "
                                f"'{pattern}'."
                            ),
                            hint=(
                                "Do not exclude production modules from coverage; "
                                "write tests instead."
                            ),
                        )
                    )

    if report_table is None:
        findings.append(
            LintFinding(
                rule="PL007",
                path=pyproject_path,
                line=1,
                col=1,
                message=(
                    "Missing [tool.coverage.report] configuration in pyproject.toml."
                ),
                hint="Configure [tool.coverage.report] with fail_under = 100.",
            )
        )
    else:
        fail_under = report_table.get("fail_under")
        if (
            isinstance(fail_under, bool)
            or not isinstance(fail_under, int | float)
            or fail_under < 100
        ):
            findings.append(
                LintFinding(
                    rule="PL007",
                    path=pyproject_path,
                    line=find_line_number(content, "fail_under"),
                    col=1,
                    message="[tool.coverage.report] must set 'fail_under = 100'.",
                    hint=(
                        "Keep fail_under = 100 under [tool.coverage.report] and "
                        "cover all branches with tests."
                    ),
                )
            )
        excludes = _as_str_list(report_table.get("exclude_also"))
        if excludes is not None:
            for pattern in excludes:
                if pattern not in _ALLOWED_COVERAGE_EXCLUDE_PATTERNS:
                    findings.append(
                        LintFinding(
                            rule="PL007",
                            path=pyproject_path,
                            line=find_line_number(content, "exclude_also"),
                            col=1,
                            message=(
                                f"[tool.coverage.report] adds disallowed "
                                f"exclude_also pattern '{pattern}'."
                            ),
                            hint=(
                                "Do not expand coverage exclude_also patterns to "
                                "bypass branch coverage."
                            ),
                        )
                    )
    return findings
