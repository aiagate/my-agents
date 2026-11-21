from ast import (
    AST,
    AnnAssign,
    Assign,
    AsyncFunctionDef,
    Call,
    ClassDef,
    Expr,
    FunctionDef,
    Import,
    ImportFrom,
    Module,
    Return,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    parse_file,
    resolve_import_targets,
)

_ALLOWED_IMPORT_PREFIX = "my_agents.composition"


def _is_allowed_import(target: str) -> bool:
    return target == _ALLOWED_IMPORT_PREFIX or target.startswith(
        f"{_ALLOWED_IMPORT_PREFIX}."
    )


def _is_delegating_body(body: list[AST]) -> bool:
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, Return):
        return True
    return isinstance(stmt, Expr) and isinstance(stmt.value, Call)


def check_thin_entrypoint(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    entrypoint = src_dir / "__init__.py"
    if not entrypoint.exists():
        return []

    tree = parse_file(entrypoint)
    findings: list[LintFinding] = []
    if not isinstance(tree, Module):
        return findings

    for stmt in tree.body:
        if isinstance(stmt, Import | ImportFrom):
            for target in resolve_import_targets(stmt, entrypoint, src_dir):
                if not _is_allowed_import(target):
                    findings.append(
                        LintFinding(
                            rule="PL014",
                            path=entrypoint,
                            line=stmt.lineno,
                            col=stmt.col_offset + 1,
                            message=(
                                f"Entrypoint '__init__.py' must not import '{target}'."
                            ),
                            hint=(
                                "Keep 'src/my_agents/__init__.py' as a thin entrypoint "
                                "that imports only from 'my_agents.composition' and "
                                "delegates immediately."
                            ),
                        )
                    )
        elif isinstance(stmt, FunctionDef | AsyncFunctionDef):
            if not _is_delegating_body(list(stmt.body)):
                findings.append(
                    LintFinding(
                        rule="PL014",
                        path=entrypoint,
                        line=stmt.lineno,
                        col=stmt.col_offset + 1,
                        message=(
                            f"Entrypoint function '{stmt.name}' must be a single-statement "
                            "delegation to 'composition'."
                        ),
                        hint=(
                            "Move startup wiring, control flow, and error handling "
                            "into 'my_agents.composition' so '__init__.py' stays thin."
                        ),
                    )
                )
        elif isinstance(stmt, Assign | AnnAssign):
            continue
        else:
            findings.append(
                LintFinding(
                    rule="PL014",
                    path=entrypoint,
                    line=stmt.lineno,
                    col=stmt.col_offset + 1,
                    message=(
                        "Entrypoint '__init__.py' must not contain top-level "
                        f"'{type(stmt).__name__}' logic."
                    ),
                    hint=(
                        "Keep 'src/my_agents/__init__.py' limited to importing and "
                        "delegating to 'my_agents.composition'."
                    ),
                )
            )

    for node in walk(tree):
        if isinstance(node, ClassDef):
            findings.append(
                LintFinding(
                    rule="PL014",
                    path=entrypoint,
                    line=node.lineno,
                    col=node.col_offset + 1,
                    message=(
                        f"Entrypoint '__init__.py' must not define class '{node.name}'."
                    ),
                    hint=("Move class definitions out of 'src/my_agents/__init__.py'."),
                )
            )
    return findings
