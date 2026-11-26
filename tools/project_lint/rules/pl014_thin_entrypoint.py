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

_ALLOWED_IMPORT_PREFIX = "my_agents.shell"


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
                            "PL014",
                            entrypoint,
                            stmt.lineno,
                            stmt.col_offset + 1,
                            f"Entrypoint '__init__.py' must not import '{target}'.",
                            "Import only from 'my_agents.shell' and delegate immediately.",
                        )
                    )
        elif isinstance(stmt, FunctionDef | AsyncFunctionDef):
            if not _is_delegating_body(list(stmt.body)):
                findings.append(
                    LintFinding(
                        "PL014",
                        entrypoint,
                        stmt.lineno,
                        stmt.col_offset + 1,
                        f"Entrypoint function '{stmt.name}' must delegate in one statement.",
                        "Move startup wiring and control flow into 'my_agents.shell.server'.",
                    )
                )
        elif isinstance(stmt, Assign | AnnAssign):
            continue
        else:
            findings.append(
                LintFinding(
                    "PL014",
                    entrypoint,
                    stmt.lineno,
                    stmt.col_offset + 1,
                    f"Entrypoint '__init__.py' must not contain '{type(stmt).__name__}'.",
                    "Keep '__init__.py' limited to importing and delegating to shell.",
                )
            )
    for node in walk(tree):
        if isinstance(node, ClassDef):
            findings.append(
                LintFinding(
                    "PL014",
                    entrypoint,
                    node.lineno,
                    node.col_offset + 1,
                    f"Entrypoint '__init__.py' must not define class '{node.name}'.",
                    "Move class definitions out of the entrypoint.",
                )
            )
    return findings
