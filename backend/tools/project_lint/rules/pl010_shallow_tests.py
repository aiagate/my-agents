from ast import (
    AST,
    AnnAssign,
    Assert,
    Assign,
    AsyncFunctionDef,
    Attribute,
    BoolOp,
    Call,
    ClassDef,
    Compare,
    Constant,
    FunctionDef,
    Is,
    IsNot,
    List,
    Name,
    Not,
    Tuple,
    UnaryOp,
    walk,
)
from collections.abc import Sequence
from pathlib import Path

from tools.project_lint.common import (
    TESTS_DIR,
    LintFinding,
    base_class_names,
    constant_is,
    has_decorator_name,
    is_protocol_class,
    iter_python_files,
    parse_file,
)


def _has_assertion_helper_call(func: FunctionDef | AsyncFunctionDef) -> bool:
    for node in walk(func):
        if not isinstance(node, Call):
            continue
        callee = node.func
        if isinstance(callee, Name) and callee.id.startswith(("assert_", "_assert_")):
            return True
        if isinstance(callee, Attribute) and (
            callee.attr.startswith(("assert_", "_assert_"))
            or callee.attr in {"raises", "warns", "fail"}
        ):
            return True
    return False


def _is_shallow_assert_expr(expr: AST) -> bool:
    if isinstance(expr, UnaryOp) and isinstance(expr.op, Not):
        return _is_shallow_assert_expr(expr.operand)
    if isinstance(expr, BoolOp):
        return all(_is_shallow_assert_expr(value) for value in expr.values)
    if isinstance(expr, Name | Attribute):
        return True
    if (
        isinstance(expr, Call)
        and isinstance(expr.func, Name)
        and expr.func.id in {"isinstance", "issubclass", "callable", "hasattr"}
    ):
        return True
    if isinstance(expr, Compare) and all(isinstance(op, Is | IsNot) for op in expr.ops):
        operands: list[AST] = [expr.left, *expr.comparators]
        return any(constant_is(operand, None) for operand in operands)
    return False


_ENUM_BASE_NAMES = frozenset({"Enum", "Flag", "IntEnum", "IntFlag", "StrEnum"})


def _parent_map(tree: AST) -> dict[int, AST]:
    parents: dict[int, AST] = {}
    for node in walk(tree):
        for child in walk(node):
            if child is node:
                continue
            parents.setdefault(id(child), node)
    return parents


def _ancestor_classes(node: AST, parents: dict[int, AST]) -> list[ClassDef]:
    classes: list[ClassDef] = []
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ClassDef):
            classes.append(current)
        current = parents.get(id(current))
    return classes


def _root_exported_names(src_dir: Path) -> set[str]:
    init = src_dir / "__init__.py"
    if not init.exists():
        return set()
    names: set[str] = set()
    for node in walk(parse_file(init)):
        targets: Sequence[AST] = []
        value: AST | None = None
        if isinstance(node, Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, AnnAssign):
            targets = [node.target]
            value = node.value
        if not any(isinstance(t, Name) and t.id == "__all__" for t in targets):
            continue
        if isinstance(value, List | Tuple):
            names.update(
                elt.value
                for elt in value.elts
                if isinstance(elt, Constant) and isinstance(elt.value, str)
            )
    return names


def _collect_public_api(
    src_dir: Path,
) -> dict[str, tuple[Path, int, int]]:
    exported = _root_exported_names(src_dir)
    api: dict[str, tuple[Path, int, int]] = {}
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        parents = _parent_map(tree)
        for node in walk(tree):
            if not isinstance(node, FunctionDef | AsyncFunctionDef):
                continue
            if node.name.startswith("_") or node.name in exported:
                continue
            if has_decorator_name(node, frozenset({"property"})):
                continue
            ancestors = _ancestor_classes(node, parents)
            if any(is_protocol_class(parent) for parent in ancestors):
                continue
            if any(
                any(base in _ENUM_BASE_NAMES for base in base_class_names(parent))
                for parent in ancestors
            ):
                continue
            api.setdefault(node.name, (path, node.lineno, node.col_offset + 1))
    return api


def _reference_names(directory: Path, names: set[str]) -> dict[str, int]:
    references: dict[str, int] = {}
    for path in iter_python_files(directory):
        for node in walk(parse_file(path)):
            if isinstance(node, Name):
                references[node.id] = references.get(node.id, 0) + 1
            elif isinstance(node, Attribute):
                references[node.attr] = references.get(node.attr, 0) + 1
            elif isinstance(node, Constant) and isinstance(node.value, str):
                for name in names:
                    if name in node.value:
                        references[name] = references.get(name, 0) + 1
    return references


def _test_only_api_findings(tests_dir: Path, src_dir: Path) -> list[LintFinding]:
    if not src_dir.exists():
        return []
    api = _collect_public_api(src_dir)
    if not api:
        return []
    names = set(api)
    src_references = _reference_names(src_dir, names)
    test_references = _reference_names(tests_dir, names)
    findings: list[LintFinding] = []
    for name, (path, line, col) in sorted(api.items()):
        if src_references.get(name, 0) > 0:
            continue
        if test_references.get(name, 0) == 0:
            continue
        findings.append(
            LintFinding(
                rule="PL010",
                path=path,
                line=line,
                col=col,
                message=(
                    f"Public API '{name}' is referenced only from tests and "
                    "never by production code."
                ),
                hint=(
                    "Remove the test-only public API or make it private; tests "
                    "must exercise production behavior rather than exist solely "
                    "to keep an unused surface alive."
                ),
            )
        )
    return findings


def check_no_shallow_tests(
    tests_dir: Path = TESTS_DIR,
    *,
    src_dir: Path | None = None,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(tests_dir):
        if not path.name.startswith("test_"):
            continue
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, FunctionDef | AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            if _has_assertion_helper_call(node):
                continue
            asserts = [child for child in walk(node) if isinstance(child, Assert)]
            if not asserts:
                findings.append(
                    LintFinding(
                        rule="PL010",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message=f"Test '{node.name}' has no assertions.",
                        hint=(
                            "Assert observable behavior, return values, or wiring "
                            "arguments in every test."
                        ),
                    )
                )
            elif all(_is_shallow_assert_expr(item.test) for item in asserts):
                first = asserts[0]
                findings.append(
                    LintFinding(
                        rule="PL010",
                        path=path,
                        line=first.lineno,
                        col=first.col_offset + 1,
                        message=(
                            f"Test '{node.name}' only checks type/existence "
                            "(isinstance/None/truthiness) without verifying values "
                            "or behavior."
                        ),
                        hint=(
                            "Verify observable behavior, state, or wiring values "
                            "(e.g. equality, content, or captured call arguments) "
                            "instead of only checking isinstance/None."
                        ),
                    )
                )
    resolved_src_dir = (
        tests_dir.parent / "src" / "my_agents" if src_dir is None else src_dir
    )
    findings.extend(_test_only_api_findings(tests_dir, resolved_src_dir))
    return findings
