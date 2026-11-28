import builtins
from ast import (
    AST,
    AsyncFunctionDef,
    Attribute,
    Call,
    ClassDef,
    ExceptHandler,
    FunctionDef,
    Name,
    Raise,
    Subscript,
    Try,
    Tuple,
    iter_child_nodes,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    base_class_name,
    dotted_name,
    iter_python_files,
    node_name,
    parse_file,
    subscript_arguments,
)

_BUILTIN_EXCEPTION_NAMES = frozenset(
    name
    for name, obj in vars(builtins).items()
    if isinstance(obj, type) and issubclass(obj, BaseException)
)
_API_EXCEPTIONS = {
    "dotenv_values": ("OSError", "UnicodeError"),
    "expanduser": ("RuntimeError",),
    "json.loads": ("JSONDecodeError",),
    "open": ("OSError",),
    "read_text": ("OSError", "UnicodeError"),
    "write": ("OSError", "UnicodeError"),
    "write_text": ("OSError", "UnicodeError"),
}


def _is_exception_base(base_name: str, known_exceptions: set[str]) -> bool:
    return (
        base_name in known_exceptions
        or base_name in _BUILTIN_EXCEPTION_NAMES
        or base_name.endswith("Exception")
    )


def _exception_class_findings(
    path: Path,
    classes: list[ClassDef],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    flagged_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name in flagged_names:
                continue
            for base in node.bases:
                name = base_class_name(base)
                if name is not None and _is_exception_base(name, flagged_names):
                    flagged_names.add(node.name)
                    findings.append(
                        LintFinding(
                            rule="PL019",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                f"Class '{node.name}' subclasses exception "
                                f"'{name}' in production code."
                            ),
                            hint=(
                                "Model errors as frozen dataclass value objects "
                                "implementing AppError(kind, message) and return "
                                "them via flow_res.Result instead of defining "
                                "Exception subclasses."
                            ),
                        )
                    )
                    changed = True
                    break
    return findings


def _subclasses(name: str) -> set[str]:
    root = getattr(builtins, name, None)
    if not isinstance(root, type):
        return set()
    found: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        for child in current.__subclasses__():
            if child.__name__ in found:
                continue
            found.add(child.__name__)
            pending.append(child)
    return found


def _caught_names(names: set[str]) -> set[str]:
    caught = set(names)
    for name in names:
        caught |= _subclasses(name)
    if "ValueError" in caught:
        caught.add("JSONDecodeError")
    return caught


def _handler_suppresses(handler: ExceptHandler) -> bool:
    return not any(isinstance(node, Raise) for node in walk(handler))


def _handler_names(handler: ExceptHandler) -> set[str]:
    if not _handler_suppresses(handler) or handler.type is None:
        return set()
    nodes = (
        list(handler.type.elts) if isinstance(handler.type, Tuple) else [handler.type]
    )
    names: set[str] = set()
    for node in nodes:
        name = node_name(node)
        if name is not None:
            names.add(name)
    return _caught_names(names)


def _raising_api(node: Call) -> str | None:
    callee = node.func
    if isinstance(callee, Name):
        return callee.id if callee.id in _API_EXCEPTIONS else None
    if not isinstance(callee, Attribute):
        return None
    if callee.attr == "loads" and dotted_name(callee.value) == "json":
        return "json.loads"
    if callee.attr in {"dotenv_values", "expanduser", "open", "read_text", "write"}:
        return callee.attr
    if callee.attr == "write_text":
        return "write_text"
    return None


def _call_exceptions(
    node: Call,
    raises_by_name: dict[str, set[str]],
) -> tuple[str | None, set[str]]:
    api = _raising_api(node)
    if api is not None:
        return api, set(_API_EXCEPTIONS[api])
    callee = node_name(node.func)
    if callee is None:
        return None, set()
    return callee, set(raises_by_name.get(callee, set()))


def _analyze_function(
    function: FunctionDef | AsyncFunctionDef,
    raises_by_name: dict[str, set[str]],
) -> tuple[list[tuple[Call, set[str]]], set[str]]:
    sites: list[tuple[Call, set[str]]] = []
    raised: set[str] = set()

    def visit(node: AST, caught: set[str]) -> None:
        nonlocal raised
        for child in iter_child_nodes(node):
            if isinstance(child, FunctionDef | AsyncFunctionDef | ClassDef):
                continue
            if isinstance(child, Try):
                handler_names: set[str] = set()
                for handler in child.handlers:
                    handler_names |= _handler_names(handler)
                for stmt in child.body:
                    visit(stmt, caught | handler_names)
                for handler in child.handlers:
                    for stmt in handler.body:
                        visit(stmt, caught)
                for stmt in child.orelse:
                    visit(stmt, caught)
                for stmt in child.finalbody:
                    visit(stmt, caught)
                continue
            if isinstance(child, Call):
                _callee, exceptions = _call_exceptions(child, raises_by_name)
                remaining = exceptions - caught
                if remaining:
                    sites.append((child, remaining))
                    raised |= remaining
            visit(child, caught)

    visit(function, set())
    return sites, raised


def _function_map(tree: AST) -> dict[str, FunctionDef | AsyncFunctionDef]:
    functions: dict[str, FunctionDef | AsyncFunctionDef] = {}
    for node in walk(tree):
        if isinstance(node, FunctionDef | AsyncFunctionDef):
            functions[node.name] = node
    return functions


def _returns_result_app_error(function: FunctionDef | AsyncFunctionDef) -> bool:
    annotation = function.returns
    if not isinstance(annotation, Subscript):
        return False
    if node_name(annotation.value) != "Result":
        return False
    arguments = subscript_arguments(annotation)
    return len(arguments) >= 2 and node_name(arguments[1]) == "AppError"


def _boundary_findings(
    path: Path,
    functions: dict[str, FunctionDef | AsyncFunctionDef],
    raises_by_name: dict[str, set[str]],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for function in functions.values():
        if not _returns_result_app_error(function):
            continue
        sites, _raised = _analyze_function(function, raises_by_name)
        for call, exceptions in sites:
            callee = node_name(call.func) or type(call.func).__name__
            findings.append(
                LintFinding(
                    rule="PL019",
                    path=path,
                    line=call.lineno,
                    col=call.col_offset + 1,
                    message=(
                        f"Result boundary '{function.name}' lets "
                        f"{', '.join(sorted(exceptions))} escape from "
                        f"'{callee}'."
                    ),
                    hint=(
                        "Catch these exceptions and return "
                        "Err(AppError(kind, message)) instead of letting them "
                        "escape a Result boundary."
                    ),
                )
            )
    return findings


def _fixpoint(
    functions: dict[str, FunctionDef | AsyncFunctionDef],
) -> dict[str, set[str]]:
    raises_by_name: dict[str, set[str]] = {name: set() for name in functions}
    changed = True
    while changed:
        changed = False
        for name, function in functions.items():
            _sites, raised = _analyze_function(function, raises_by_name)
            if raised - raises_by_name[name]:
                raises_by_name[name] |= raised
                changed = True
    return raises_by_name


def check_no_exception_result_errors(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        classes = [node for node in walk(tree) if isinstance(node, ClassDef)]
        findings.extend(_exception_class_findings(path, classes))
        functions = _function_map(tree)
        if not functions:
            continue
        findings.extend(_boundary_findings(path, functions, _fixpoint(functions)))
    return findings
