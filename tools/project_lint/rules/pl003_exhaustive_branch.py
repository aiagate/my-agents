from ast import (
    AST,
    AnnAssign,
    Attribute,
    Call,
    ClassDef,
    Compare,
    Constant,
    If,
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


def _is_defensive_else(orelse: list[AST]) -> bool:
    if len(orelse) != 1:
        return False
    stmt = orelse[0]
    if isinstance(stmt, Raise):
        return True
    for child in walk(stmt):
        if isinstance(child, Call):
            callee = child.func
            if isinstance(callee, Name) and callee.id == "assert_never":
                return True
            if isinstance(callee, Attribute) and callee.attr == "assert_never":
                return True
    return False


def check_exhaustive_branch(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    literal_attrs, enum_names = _collect_closed_types(src_dir)
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        elif_nodes: set[int] = set()
        for node in walk(tree):
            if (
                isinstance(node, If)
                and len(node.orelse) == 1
                and isinstance(node.orelse[0], If)
            ):
                elif_nodes.add(id(node.orelse[0]))

        for node in walk(tree):
            if not isinstance(node, If) or id(node) in elif_nodes:
                continue
            target_name = _compare_targets_closed_type(
                node.test, literal_attrs, enum_names
            )
            if target_name is None:
                continue
            current = node
            while len(current.orelse) == 1 and isinstance(current.orelse[0], If):
                current = current.orelse[0]
            if current.orelse and not _is_defensive_else(list(current.orelse)):
                findings.append(
                    LintFinding(
                        rule="PL003",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message=(
                            f"Non-exhaustive 'if ... else:' branch on closed type "
                            f"'{target_name}'."
                        ),
                        hint=(
                            "Use 'match' with 'typing.assert_never' so pyright "
                            "catches unhandled values if new Literal/Enum members "
                            "are added."
                        ),
                    )
                )
    return findings
