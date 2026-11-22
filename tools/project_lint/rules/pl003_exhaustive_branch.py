from ast import (
    AST,
    AnnAssign,
    Attribute,
    Call,
    ClassDef,
    Compare,
    Constant,
    If,
    IfExp,
    Name,
    Raise,
    Subscript,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)


def _annotation_is_literal(annotation: AST | None) -> bool:
    if not isinstance(annotation, Subscript):
        return False
    value = annotation.value
    if isinstance(value, Name) and value.id == "Literal":
        return True
    return isinstance(value, Attribute) and value.attr == "Literal"


def _collect_closed_types(src_dir: Path) -> tuple[set[str], set[str]]:
    literal_attrs: set[str] = set()
    enum_names: set[str] = set()
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if isinstance(node, ClassDef):
                for base in node.bases:
                    base_name = (
                        base.id
                        if isinstance(base, Name)
                        else base.attr
                        if isinstance(base, Attribute)
                        else ""
                    )
                    if base_name in {"Enum", "StrEnum", "IntEnum"}:
                        enum_names.add(node.name)
                for stmt in node.body:
                    if (
                        isinstance(stmt, AnnAssign)
                        and isinstance(stmt.target, Name)
                        and _annotation_is_literal(stmt.annotation)
                    ):
                        literal_attrs.add(stmt.target.id)
    return literal_attrs, enum_names


def _compare_targets_closed_type(
    test: AST,
    literal_attrs: set[str],
    enum_names: set[str],
) -> str | None:
    if not isinstance(test, Compare):
        return None
    operands: list[AST] = [test.left, *test.comparators]
    for operand in operands:
        if (
            isinstance(operand, Attribute)
            and operand.attr in literal_attrs
            and any(isinstance(other, Constant) for other in operands)
        ):
            return operand.attr
        if (
            isinstance(operand, Attribute)
            and isinstance(operand.value, Name)
            and operand.value.id in enum_names
        ):
            return operand.value.id
    return None


def _expr_calls_assert_never(expr: AST) -> bool:
    for child in walk(expr):
        if isinstance(child, Call):
            callee = child.func
            if isinstance(callee, Name) and callee.id == "assert_never":
                return True
            if isinstance(callee, Attribute) and callee.attr == "assert_never":
                return True
    return False


def _is_defensive_else(orelse: list[AST]) -> bool:
    if len(orelse) != 1:
        return False
    stmt = orelse[0]
    return isinstance(stmt, Raise) or _expr_calls_assert_never(stmt)


def check_exhaustive_branch(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    literal_attrs, enum_names = _collect_closed_types(src_dir)
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        elif_nodes: set[int] = set()
        nested_ifexp_nodes: set[int] = set()
        for node in walk(tree):
            if (
                isinstance(node, If)
                and len(node.orelse) == 1
                and isinstance(node.orelse[0], If)
            ):
                elif_nodes.add(id(node.orelse[0]))
            elif isinstance(node, IfExp) and isinstance(node.orelse, IfExp):
                nested_ifexp_nodes.add(id(node.orelse))

        for node in walk(tree):
            if (
                not isinstance(node, If | IfExp)
                or id(node) in elif_nodes
                or id(node) in nested_ifexp_nodes
            ):
                continue
            target_name = _compare_targets_closed_type(
                node.test, literal_attrs, enum_names
            )
            if target_name is None:
                continue
            if isinstance(node, If):
                current_if = node
                while len(current_if.orelse) == 1 and isinstance(
                    current_if.orelse[0], If
                ):
                    current_if = current_if.orelse[0]
                if not current_if.orelse or _is_defensive_else(list(current_if.orelse)):
                    continue
                branch = "'if ... else:' branch"
            else:
                current_ifexp = node
                while isinstance(current_ifexp.orelse, IfExp):
                    current_ifexp = current_ifexp.orelse
                if _expr_calls_assert_never(current_ifexp.orelse):
                    continue
                branch = "'... if ... else ...' expression"
            findings.append(
                LintFinding(
                    rule="PL003",
                    path=path,
                    line=node.lineno,
                    col=node.col_offset + 1,
                    message=f"Non-exhaustive {branch} on closed type '{target_name}'.",
                    hint=(
                        "Use 'match' with 'typing.assert_never' so pyright "
                        "catches unhandled values if new Literal/Enum members "
                        "are added."
                    ),
                )
            )
    return findings
