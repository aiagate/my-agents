from ast import (
    AST,
    Assert,
    AsyncFunctionDef,
    Attribute,
    BoolOp,
    Call,
    Compare,
    FunctionDef,
    Is,
    IsNot,
    Name,
    Not,
    UnaryOp,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    TESTS_DIR,
    LintFinding,
    constant_is,
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


def check_no_shallow_tests(tests_dir: Path = TESTS_DIR) -> list[LintFinding]:
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
    return findings
