from ast import (
    AST,
    AnnAssign,
    Attribute,
    Call,
    ClassDef,
    Constant,
    FunctionDef,
    Name,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    annotation_is_non_init_marker,
    iter_python_files,
    parse_file,
)


def _is_dataclass_with_generated_init(node: ClassDef) -> bool:
    has_dataclass_decorator = False
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, Call) else decorator
        is_dataclass = (isinstance(target, Name) and target.id == "dataclass") or (
            isinstance(target, Attribute) and target.attr == "dataclass"
        )
        if not is_dataclass:
            continue
        if isinstance(decorator, Call):
            for kw in decorator.keywords:
                if (
                    kw.arg == "init"
                    and isinstance(kw.value, Constant)
                    and kw.value.value is False
                ):
                    return False
        has_dataclass_decorator = True
    if not has_dataclass_decorator:
        return False
    return not any(
        isinstance(stmt, FunctionDef) and stmt.name == "__init__" for stmt in node.body
    )


def _field_disables_init(value: AST | None) -> bool:
    if not isinstance(value, Call):
        return False
    callee = value.func
    is_field = (isinstance(callee, Name) and callee.id == "field") or (
        isinstance(callee, Attribute) and callee.attr == "field"
    )
    if not is_field:
        return False
    return any(
        kw.arg == "init" and isinstance(kw.value, Constant) and kw.value.value is False
        for kw in value.keywords
    )


def check_no_private_dataclass_init_fields(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, ClassDef):
                continue
            if not _is_dataclass_with_generated_init(node):
                continue
            for stmt in node.body:
                if not isinstance(stmt, AnnAssign) or not isinstance(stmt.target, Name):
                    continue
                field_name = stmt.target.id
                if not field_name.startswith("_"):
                    continue
                if field_name.startswith("__") and field_name.endswith("__"):
                    continue
                if annotation_is_non_init_marker(stmt.annotation):
                    continue
                if _field_disables_init(stmt.value):
                    continue
                findings.append(
                    LintFinding(
                        rule="PL011",
                        path=path,
                        line=stmt.lineno,
                        col=stmt.col_offset + 1,
                        message=(
                            f"Dataclass '{node.name}' exposes underscore-prefixed "
                            f"field '{field_name}' as a public __init__ parameter."
                        ),
                        hint=(
                            "Use a public field name, mark it field(init=False), or "
                            "define an explicit positional-only __init__ (e.g. "
                            "'def __init__(self, value: str, /)') instead of "
                            "exposing '_'-prefixed parameters. For Secret Value "
                            "Objects, use a plain non-dataclass class with "
                            "'__slots__' instead."
                        ),
                    )
                )
    return findings
