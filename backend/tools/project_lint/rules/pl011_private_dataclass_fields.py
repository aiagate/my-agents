from ast import AST, AnnAssign, Call, ClassDef, FunctionDef, Name, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    annotation_is_non_init_marker,
    call_name,
    constant_is,
    dataclass_keyword,
    has_dataclass_decorator,
    iter_python_files,
    keyword_value,
    parse_file,
)


def _is_dataclass_with_generated_init(node: ClassDef) -> bool:
    if not has_dataclass_decorator(node):
        return False
    if constant_is(dataclass_keyword(node, "init"), False):
        return False
    return not any(
        isinstance(stmt, FunctionDef) and stmt.name == "__init__" for stmt in node.body
    )


def _field_disables_init(value: AST | None) -> bool:
    if not isinstance(value, Call) or call_name(value) != "field":
        return False
    return constant_is(keyword_value(value, "init"), False)


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
