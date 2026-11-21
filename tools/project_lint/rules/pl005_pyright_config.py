import tomllib
from pathlib import Path

from tools.project_lint.common import (
    PROJECT_ROOT,
    LintFinding,
    as_table,
    find_line_number,
)

_FORBIDDEN_PYRIGHT_FALSE_FLAGS = (
    "reportUnknownVariableType",
    "reportUnknownParameterType",
    "reportUnknownMemberType",
    "reportUnknownArgumentType",
    "reportPrivateUsage",
)


def check_strict_pyright_config(
    pyproject_path: Path = PROJECT_ROOT / "pyproject.toml",
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    content = pyproject_path.read_text(encoding="utf-8")
    root_table = as_table(tomllib.loads(content))
    tool_table = as_table(root_table.get("tool")) if root_table is not None else None
    pyright_table = (
        as_table(tool_table.get("pyright")) if tool_table is not None else None
    )
    if pyright_table is None:
        findings.append(
            LintFinding(
                rule="PL005",
                path=pyproject_path,
                line=1,
                col=1,
                message="Missing [tool.pyright] configuration in pyproject.toml.",
                hint="Configure [tool.pyright] with typeCheckingMode = 'strict'.",
            )
        )
        return findings

    if pyright_table.get("typeCheckingMode") != "strict":
        findings.append(
            LintFinding(
                rule="PL005",
                path=pyproject_path,
                line=find_line_number(content, "typeCheckingMode"),
                col=1,
                message="pyright typeCheckingMode must be 'strict'.",
                hint="Set typeCheckingMode = 'strict' under [tool.pyright].",
            )
        )

    for flag in _FORBIDDEN_PYRIGHT_FALSE_FLAGS:
        if pyright_table.get(flag) is False:
            findings.append(
                LintFinding(
                    rule="PL005",
                    path=pyproject_path,
                    line=find_line_number(content, flag),
                    col=1,
                    message=f"[tool.pyright] disables strict check '{flag} = false'.",
                    hint=(
                        "Remove blanket pyright suppressions from pyproject.toml "
                        "and fix the underlying types instead."
                    ),
                )
            )
    return findings
