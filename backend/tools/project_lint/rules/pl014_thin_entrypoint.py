import tomllib
from ast import (
    AST,
    AnnAssign,
    Assign,
    AsyncFunctionDef,
    Attribute,
    Call,
    ClassDef,
    Expr,
    FunctionDef,
    Import,
    ImportFrom,
    Module,
    Name,
    Return,
    iter_child_nodes,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    as_table,
    dotted_name,
    iter_python_files,
    parse_file,
    resolve_import_targets,
)

_ENTRYPOINT_IMPORT_PREFIX = "my_agents.shell"
_IO_CALL_NAMES = frozenset(
    {"open", "read_bytes", "read_text", "write_bytes", "write_text"}
)


def _is_allowed_import(target: str) -> bool:
    return target == _ENTRYPOINT_IMPORT_PREFIX or target.startswith(
        f"{_ENTRYPOINT_IMPORT_PREFIX}."
    )


def _is_delegating_body(body: list[AST]) -> bool:
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, Return):
        return True
    return isinstance(stmt, Expr) and isinstance(stmt.value, Call)


def _is_all_assignment(stmt: Assign | AnnAssign) -> bool:
    targets = stmt.targets if isinstance(stmt, Assign) else [stmt.target]
    return any(
        isinstance(target, Name) and target.id == "__all__" for target in targets
    )


def _entrypoint_import_finding(
    path: Path,
    stmt: Import | ImportFrom,
    target: str,
) -> LintFinding:
    return LintFinding(
        "PL014",
        path,
        stmt.lineno,
        stmt.col_offset + 1,
        f"Entrypoint '__init__.py' must not import '{target}'.",
        "Import only from 'my_agents.shell' and delegate immediately.",
    )


def _init_structure_findings(
    path: Path,
    src_dir: Path,
    *,
    is_root: bool,
) -> list[LintFinding]:
    tree = parse_file(path)
    findings: list[LintFinding] = []
    if not isinstance(tree, Module):
        return findings
    for stmt in tree.body:
        if isinstance(stmt, Import | ImportFrom):
            if not is_root:
                continue
            for target in resolve_import_targets(stmt, path, src_dir):
                if not _is_allowed_import(target):
                    findings.append(_entrypoint_import_finding(path, stmt, target))
        elif isinstance(stmt, Assign | AnnAssign):
            if _is_all_assignment(stmt):
                continue
            findings.append(
                LintFinding(
                    "PL014",
                    path,
                    stmt.lineno,
                    stmt.col_offset + 1,
                    "Entrypoint '__init__.py' may only assign '__all__'.",
                    "Keep '__init__.py' limited to imports, '__all__', and thin delegation.",
                )
            )
        elif isinstance(stmt, FunctionDef | AsyncFunctionDef):
            if not _is_delegating_body(list(stmt.body)):
                findings.append(
                    LintFinding(
                        "PL014",
                        path,
                        stmt.lineno,
                        stmt.col_offset + 1,
                        f"Entrypoint function '{stmt.name}' must delegate in one statement.",
                        "Move startup wiring and control flow into 'my_agents.shell.server'.",
                    )
                )
        elif isinstance(stmt, ClassDef):
            continue
        else:
            findings.append(
                LintFinding(
                    "PL014",
                    path,
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
                    path,
                    node.lineno,
                    node.col_offset + 1,
                    f"Entrypoint '__init__.py' must not define class '{node.name}'.",
                    "Move class definitions out of the entrypoint.",
                )
            )
    return findings


def _module_path(src_dir: Path, dotted_module: str) -> Path | None:
    if dotted_module == "my_agents":
        return src_dir / "__init__.py"
    prefix = "my_agents."
    if not dotted_module.startswith(prefix):
        return None
    relative = dotted_module.removeprefix(prefix)
    base = src_dir.joinpath(*relative.split("."))
    if base.is_dir():
        return base / "__init__.py"
    candidate = base.with_suffix(".py")
    return candidate if candidate.exists() else None


def _resolve_export(
    module_file: Path,
    attribute: str,
    src_dir: Path,
    visited: set[tuple[Path, str]],
) -> tuple[Path, FunctionDef | AsyncFunctionDef] | None:
    if module_file.is_dir():
        module_file = module_file / "__init__.py"
    key = (module_file, attribute)
    if key in visited or not module_file.exists():
        return None
    visited.add(key)
    tree = parse_file(module_file)
    if not isinstance(tree, Module):
        return None
    for stmt in tree.body:
        if isinstance(stmt, FunctionDef | AsyncFunctionDef) and stmt.name == attribute:
            return module_file, stmt
    for stmt in tree.body:
        if not isinstance(stmt, ImportFrom):
            continue
        for alias in stmt.names:
            bound = alias.asname or alias.name
            if bound != attribute:
                continue
            targets = resolve_import_targets(stmt, module_file, src_dir)
            if not targets:
                continue
            imported_file = _module_path(src_dir, targets[0])
            if imported_file is None:
                continue
            return _resolve_export(imported_file, alias.name, src_dir, visited)
    return None


def _script_findings(
    pyproject_path: Path,
    src_dir: Path,
) -> list[LintFinding]:
    if not pyproject_path.exists():
        return []
    data = as_table(tomllib.loads(pyproject_path.read_text(encoding="utf-8")))
    project = as_table(data.get("project")) if data is not None else None
    scripts = as_table(project.get("scripts")) if project is not None else None
    if scripts is None:
        return []
    findings: list[LintFinding] = []
    for name, target in scripts.items():
        if not isinstance(target, str) or ":" not in target:
            continue
        module_part, attribute = target.split(":", 1)
        module_file = _module_path(src_dir, module_part)
        resolved = (
            None
            if module_file is None
            else _resolve_export(module_file, attribute, src_dir, set())
        )
        if resolved is None:
            findings.append(
                LintFinding(
                    "PL014",
                    pyproject_path,
                    _line_of(pyproject_path.read_text(encoding="utf-8"), target),
                    1,
                    f"Script '{name}' target '{target}' cannot be resolved to a function.",
                    "Point '[project.scripts]' at a thin exported function under 'src/my_agents'.",
                )
            )
            continue
        function_file, function = resolved
        span = (function.end_lineno or function.lineno) - function.lineno + 1
        if len(function.body) > 1 and span > 25:
            findings.append(
                LintFinding(
                    "PL014",
                    function_file,
                    function.lineno,
                    function.col_offset + 1,
                    f"Script target '{target}' is too large ({span} lines, {len(function.body)} statements).",
                    "Delegate in one statement or keep the entrypoint function at 25 lines or fewer.",
                )
            )
    return findings


def _line_of(content: str, token: str) -> int:
    for index, line in enumerate(content.splitlines(), start=1):
        if token in line:
            return index
    return 1


def _module_level_io_nodes(tree: Module) -> list[Call]:
    calls: list[Call] = []

    def visit(node: AST) -> None:
        for child in iter_child_nodes(node):
            if isinstance(child, FunctionDef | AsyncFunctionDef | ClassDef):
                continue
            if isinstance(child, Call) and _is_io_call(child):
                calls.append(child)
            visit(child)

    visit(tree)
    return calls


def _is_io_call(node: Call) -> bool:
    if isinstance(node.func, Name):
        return node.func.id in _IO_CALL_NAMES
    if not isinstance(node.func, Attribute):
        return False
    if node.func.attr in _IO_CALL_NAMES:
        return True
    return node.func.attr == "connect" and dotted_name(node.func.value) == "sqlite3"


def check_thin_entrypoint(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        if path.name != "__init__.py":
            continue
        findings.extend(
            _init_structure_findings(
                path,
                src_dir,
                is_root=path == src_dir / "__init__.py",
            )
        )
    root = src_dir.parents[1]
    findings.extend(_script_findings(root / "pyproject.toml", src_dir))
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        if not isinstance(tree, Module):
            continue
        for node in _module_level_io_nodes(tree):
            findings.append(
                LintFinding(
                    "PL014",
                    path,
                    node.lineno,
                    node.col_offset + 1,
                    "Module-level I/O runs during import and must move inside a function.",
                    "Load files or connect to SQLite lazily so importing a module has no side effects.",
                )
            )
    return findings
