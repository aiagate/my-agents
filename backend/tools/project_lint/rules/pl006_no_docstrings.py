from ast import (
    AST,
    AnnAssign,
    Assign,
    AsyncFunctionDef,
    ClassDef,
    Constant,
    Expr,
    FunctionDef,
    Module,
    Name,
    walk,
)
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import TypeIs

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    TESTS_DIR,
    TOOLS_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)

_DOCSTRING_HINT = (
    "Remove the docstring and express the contract via strict types, naming, "
    "and tests (use '...' for empty abstract/protocol bodies). Do not replace "
    "it with redundant '#' comments; reserve '#' for non-obvious 'why' notes."
)


def _is_str_expr(node: AST) -> TypeIs[Expr]:
    return (
        isinstance(node, Expr)
        and isinstance(node.value, Constant)
        and isinstance(node.value.value, str)
    )


def _assignment_target_name(node: Assign | AnnAssign) -> str:
    if isinstance(node, AnnAssign) and isinstance(node.target, Name):
        return node.target.id
    if (
        isinstance(node, Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], Name)
    ):
        return node.targets[0].id
    return "<attribute>"


def _check_attribute_docstrings(
    path: Path,
    body: Sequence[AST],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for prev, curr in pairwise(body):
        if isinstance(prev, Assign | AnnAssign) and _is_str_expr(curr):
            attr_name = _assignment_target_name(prev)
            findings.append(
                LintFinding(
                    rule="PL006",
                    path=path,
                    line=curr.lineno,
                    col=curr.col_offset + 1,
                    message=f"Attribute '{attr_name}' must not have a docstring.",
                    hint=_DOCSTRING_HINT,
                )
            )
    return findings


def check_no_docstrings(
    directories: Sequence[Path] = (SRC_PACKAGE_DIR, TESTS_DIR, TOOLS_DIR),
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for directory in directories:
        for path in iter_python_files(directory):
            tree = parse_file(path)
            for node in walk(tree):
                if isinstance(node, Module):
                    if node.body and _is_str_expr(node.body[0]):
                        first = node.body[0]
                        findings.append(
                            LintFinding(
                                rule="PL006",
                                path=path,
                                line=first.lineno,
                                col=first.col_offset + 1,
                                message="Module must not have a docstring.",
                                hint=_DOCSTRING_HINT,
                            )
                        )
                    findings.extend(_check_attribute_docstrings(path, node.body))
                elif isinstance(node, ClassDef):
                    if node.body and _is_str_expr(node.body[0]):
                        first = node.body[0]
                        findings.append(
                            LintFinding(
                                rule="PL006",
                                path=path,
                                line=first.lineno,
                                col=first.col_offset + 1,
                                message=f"Class '{node.name}' must not have a docstring.",
                                hint=_DOCSTRING_HINT,
                            )
                        )
                    findings.extend(_check_attribute_docstrings(path, node.body))
                elif (
                    isinstance(node, FunctionDef | AsyncFunctionDef)
                    and node.body
                    and _is_str_expr(node.body[0])
                ):
                    first = node.body[0]
                    findings.append(
                        LintFinding(
                            rule="PL006",
                            path=path,
                            line=first.lineno,
                            col=first.col_offset + 1,
                            message=(
                                f"Function '{node.name}' must not have a docstring."
                            ),
                            hint=_DOCSTRING_HINT,
                        )
                    )
    return findings
