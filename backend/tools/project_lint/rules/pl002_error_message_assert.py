from ast import (
    AST,
    AnnAssign,
    Assert,
    AsyncFunctionDef,
    Attribute,
    Call,
    ClassDef,
    Compare,
    Constant,
    Eq,
    FunctionDef,
    GtE,
    Name,
    walk,
)
from ast import (
    dump as ast_dump,
)
from pathlib import Path
from typing import TypeIs

from tools.project_lint.common import (
    TESTS_DIR,
    LintFinding,
    call_name,
    constant_is,
    has_decorator_name,
    is_protocol_class,
    iter_python_files,
    keyword_value,
    node_name,
    parse_file,
)


def _collect_displayable_error_symbols(
    src_dir: Path,
) -> tuple[set[str], set[str]]:
    displayable_classes: set[str] = set()
    displayable_error_types: set[str] = set()
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, ClassDef) or is_protocol_class(node):
                continue
            has_display_message = False
            error_type_name: str | None = None
            for stmt in node.body:
                if isinstance(stmt, AnnAssign) and isinstance(stmt.target, Name):
                    if stmt.target.id == "display_message":
                        has_display_message = True
                    elif stmt.target.id in {"type", "kind"}:
                        error_type_name = node_name(stmt.annotation)
                elif isinstance(
                    stmt, FunctionDef | AsyncFunctionDef
                ) and has_decorator_name(stmt, frozenset({"property"})):
                    if stmt.name == "display_message":
                        has_display_message = True
                    elif stmt.name == "type":
                        error_type_name = node_name(stmt.returns)
            if has_display_message:
                displayable_classes.add(node.name)
                if error_type_name is not None:
                    displayable_error_types.add(error_type_name)
    return displayable_classes, displayable_error_types


def _collect_typed_error_symbols(
    src_dir: Path,
) -> tuple[set[str], set[str]]:
    typed_classes: set[str] = set()
    typed_error_types: set[str] = set()
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, ClassDef) or is_protocol_class(node):
                continue
            has_message = False
            error_type_name: str | None = None
            for stmt in node.body:
                if isinstance(stmt, AnnAssign) and isinstance(stmt.target, Name):
                    if stmt.target.id == "message":
                        has_message = True
                    elif stmt.target.id in {"type", "kind"}:
                        error_type_name = node_name(stmt.annotation)
                elif isinstance(
                    stmt, FunctionDef | AsyncFunctionDef
                ) and has_decorator_name(stmt, frozenset({"property"})):
                    if stmt.name == "message":
                        has_message = True
                    elif stmt.name == "type":
                        error_type_name = node_name(stmt.returns)
            if has_message and error_type_name is not None:
                typed_classes.add(node.name)
                typed_error_types.add(error_type_name)
    return typed_classes, typed_error_types


def _is_error_assert_helper_call(node: AST) -> TypeIs[Call]:
    return call_name(node) in {"assert_err", "assert_error"}


def _assert_checks_error_type(assert_node: Assert) -> bool:
    for node in walk(assert_node.test):
        if isinstance(node, Attribute) and node.attr == "type":
            return True
    return False


def _helper_has_expected_type(call: Call) -> bool:
    expected = keyword_value(call, "expected_type")
    if expected is None:
        expected = keyword_value(call, "kind")
    return expected is not None and not constant_is(expected, None)


def _test_verifies_error_message(func: FunctionDef | AsyncFunctionDef) -> bool:
    for node in walk(func):
        if _is_error_assert_helper_call(node):
            return True
        if isinstance(node, Assert):
            for child in walk(node.test):
                if isinstance(child, Attribute) and child.attr in {
                    "message",
                    "display_message",
                }:
                    return True
                if (
                    isinstance(child, Call)
                    and isinstance(child.func, Name)
                    and child.func.id == "str"
                ):
                    return True
    return False


def _test_verifies_display_message(func: FunctionDef | AsyncFunctionDef) -> bool:
    for node in walk(func):
        if isinstance(node, Call) and _is_error_assert_helper_call(node):
            display_message = keyword_value(node, "display_message_contains")
            if display_message is not None and not constant_is(display_message, None):
                return True
        if isinstance(node, Assert):
            for child in walk(node.test):
                if isinstance(child, Attribute) and child.attr == "display_message":
                    return True
    return False


_BODY_ACCESSORS = frozenset({"json", "text", "content", "body"})


def _status_code_subject(compare: Compare) -> AST | None:
    operands = [compare.left, *compare.comparators]
    for operand in operands:
        if isinstance(operand, Attribute) and operand.attr == "status_code":
            return operand.value
    return None


def _is_error_status_comparison(compare: Compare) -> bool:
    operands = [compare.left, *compare.comparators]
    for index, op in enumerate(compare.ops):
        left = operands[index]
        right = operands[index + 1]
        constant: Constant | None = None
        if isinstance(right, Constant):
            constant = right
        elif isinstance(left, Constant):
            constant = left
        if constant is None or not isinstance(constant.value, int):
            continue
        value = constant.value
        if isinstance(op, GtE | Eq) and value >= 400:
            return True
        if isinstance(op, Eq) and 400 <= value <= 599:
            return True
    return False


def _expr_identifiers(node: AST) -> set[str]:
    return {child.id for child in walk(node) if isinstance(child, Name)}


def _body_accesses_subject(node: AST, subject: AST) -> bool:
    subject_names = _expr_identifiers(subject)
    subject_dump = ast_dump(subject)
    for child in walk(node):
        if not isinstance(child, Attribute) or child.attr not in _BODY_ACCESSORS:
            continue
        if _expr_identifiers(child.value) & subject_names:
            return True
        if ast_dump(child.value) == subject_dump:
            return True
    return False


def _test_verifies_response_body(
    func: FunctionDef | AsyncFunctionDef,
    subject: AST,
) -> bool:
    return any(
        _body_accesses_subject(child, subject)
        for child in walk(func)
        if isinstance(child, Assert)
    )


def _http_status_findings(
    func: FunctionDef | AsyncFunctionDef,
    path: Path,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for child in walk(func):
        if not isinstance(child, Assert):
            continue
        for compare in walk(child.test):
            if not isinstance(compare, Compare):
                continue
            subject = _status_code_subject(compare)
            if subject is None or not _is_error_status_comparison(compare):
                continue
            if _test_verifies_response_body(func, subject):
                continue
            findings.append(
                LintFinding(
                    rule="PL002",
                    path=path,
                    line=child.lineno,
                    col=child.col_offset + 1,
                    message=(
                        f"Test '{func.name}' checks only an HTTP error "
                        "status without verifying the response body."
                    ),
                    hint=(
                        "Also assert the error payload from '.json()', '.text', "
                        "'.content', or '.body' so the AppError kind and message "
                        "are verified, not just the status code."
                    ),
                )
            )
            break
    return findings


def _references_any_symbol(
    func: FunctionDef | AsyncFunctionDef,
    symbols: set[str],
) -> bool:
    if not symbols:
        return False
    for node in walk(func):
        if isinstance(node, Name) and node.id in symbols:
            return True
        if isinstance(node, Attribute) and node.attr in symbols:
            return True
    return False


def check_assert_error_message(
    tests_dir: Path = TESTS_DIR,
    *,
    src_dir: Path | None = None,
) -> list[LintFinding]:
    resolved_src_dir = (
        tests_dir.parent / "src" / "my_agents" if src_dir is None else src_dir
    )
    displayable_classes, displayable_error_types = _collect_displayable_error_symbols(
        resolved_src_dir
    )
    displayable_symbols = displayable_classes | displayable_error_types
    typed_classes, typed_error_types = _collect_typed_error_symbols(resolved_src_dir)
    typed_symbols = typed_classes | typed_error_types

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
            type_asserts = [
                child
                for child in walk(node)
                if isinstance(child, Assert) and _assert_checks_error_type(child)
            ]
            helper_calls = [
                child for child in walk(node) if _is_error_assert_helper_call(child)
            ]
            findings.extend(_http_status_findings(node, path))

            if type_asserts and not _test_verifies_error_message(node):
                first = type_asserts[0]
                findings.append(
                    LintFinding(
                        rule="PL002",
                        path=path,
                        line=first.lineno,
                        col=first.col_offset + 1,
                        message=(
                            f"Test '{node.name}' asserts '.type'/'.kind' without verifying "
                            "the error message."
                        ),
                        hint=(
                            "Also assert 'error.message' or 'error.display_message', "
                            "or use an assert_err(..., message_contains=...) helper so "
                            "message propagation is always checked."
                        ),
                    )
                )
                continue

            missing_expected_type = [
                call for call in helper_calls if not _helper_has_expected_type(call)
            ]
            if missing_expected_type and _references_any_symbol(node, typed_symbols):
                anchor_call = missing_expected_type[0]
                findings.append(
                    LintFinding(
                        rule="PL002",
                        path=path,
                        line=anchor_call.lineno,
                        col=anchor_call.col_offset + 1,
                        message=(
                            f"Test '{node.name}' asserts an error type without a "
                            "non-None 'expected_type' in assert_err(...)."
                        ),
                        hint=(
                            "Pass expected_type=<ErrorType.VALUE> to assert_err(...) "
                            "so the error type is verified by the helper itself."
                        ),
                    )
                )
                continue

            if (
                (type_asserts or helper_calls)
                and _references_any_symbol(node, displayable_symbols)
                and not _test_verifies_display_message(node)
            ):
                anchor: Call | Assert = (
                    helper_calls[0] if helper_calls else type_asserts[0]
                )
                findings.append(
                    LintFinding(
                        rule="PL002",
                        path=path,
                        line=anchor.lineno,
                        col=anchor.col_offset + 1,
                        message=(
                            f"Test '{node.name}' asserts a display message without "
                            "verifying '.display_message'."
                        ),
                        hint=(
                            "Also assert 'error.display_message' or pass "
                            "'display_message_contains=...' to assert_err(...)."
                        ),
                    )
                )
    return findings
