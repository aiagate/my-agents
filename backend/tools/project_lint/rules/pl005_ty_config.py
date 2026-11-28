import tomllib
from pathlib import Path

from tools.project_lint.common import (
    PROJECT_ROOT,
    LintFinding,
    as_table,
    find_line_number,
)


def check_strict_ty_config(
    pyproject_path: Path = PROJECT_ROOT / "pyproject.toml",
) -> list[LintFinding]:
    content = pyproject_path.read_text(encoding="utf-8")
    tool = as_table(tomllib.loads(content).get("tool")) or {}
    ty = as_table(tool.get("ty")) or {}
    terminal = as_table(ty.get("terminal")) or {}
    rules = as_table(ty.get("rules")) or {}
    findings: list[LintFinding] = []

    if terminal.get("error-on-warning") is not True:
        findings.append(
            LintFinding(
                rule="PL005",
                path=pyproject_path,
                line=find_line_number(content, "error-on-warning"),
                col=1,
                message="ty must fail on warning diagnostics.",
                hint="Set error-on-warning = true under [tool.ty.terminal].",
            )
        )

    for rule, severity in rules.items():
        if severity == "ignore":
            findings.append(
                LintFinding(
                    rule="PL005",
                    path=pyproject_path,
                    line=find_line_number(content, rule),
                    col=1,
                    message=f"[tool.ty.rules] disables diagnostic '{rule}'.",
                    hint="Remove the ignore setting and fix the underlying types.",
                )
            )
    return findings
