from ast import (
    AST,
    AsyncFunctionDef,
    Attribute,
    Call,
    Constant,
    FunctionDef,
    Import,
    ImportFrom,
    Name,
    Subscript,
    Tuple,
    walk,
)
from pathlib import Path
from typing import TypeIs

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)

_TOOL_MODULES = frozenset({"pydantic_ai", "pydantic_ai.tools"})


def _collect_tool_names(tree: AST) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    module_prefixes: set[str] = set()
    for node in walk(tree):
        if isinstance(node, ImportFrom) and node.module in _TOOL_MODULES:
            for alias in node.names:
                if alias.name == "Tool":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, Import):
            for alias in node.names:
                if alias.name == "pydantic_ai":
                    module_prefixes.add(alias.asname or "pydantic_ai")
                elif alias.name == "pydantic_ai.tools":
                    module_prefixes.add(alias.asname or "pydantic_ai.tools")
    return names, module_prefixes


def _is_tool_call(
    node: AST,
    names: set[str],
    module_prefixes: set[str],
) -> TypeIs[Call]:
    if not isinstance(node, Call):
        return False
    callee = node.func
    if isinstance(callee, Name):
        return callee.id in names
    if isinstance(callee, Attribute) and callee.attr == "Tool":
        return isinstance(callee.value, Name) and callee.value.id in module_prefixes
    return False


def _is_run_context(annotation: AST) -> bool:
    target = annotation.value if isinstance(annotation, Subscript) else annotation
    if isinstance(target, Name):
        return target.id == "RunContext"
    if isinstance(target, Attribute):
        return target.attr == "RunContext"
    return False


def _is_field_call(node: AST) -> TypeIs[Call]:
    if not isinstance(node, Call):
        return False
    callee = node.func
    if isinstance(callee, Name):
        return callee.id == "Field"
    return isinstance(callee, Attribute) and callee.attr == "Field"


def _has_field_description(annotation: AST) -> bool:
    if not isinstance(annotation, Subscript):
        return False
    target = annotation.value
    if isinstance(target, Name):
        if target.id != "Annotated":
            return False
    elif isinstance(target, Attribute):
        if target.attr != "Annotated":
            return False
    else:
        return False
    metadata = (
        list(annotation.slice.elts)
        if isinstance(annotation.slice, Tuple)
        else [annotation.slice]
    )
    for item in metadata[1:]:
        if not _is_field_call(item):
            continue
        for kw in item.keywords:
            if kw.arg != "description":
                continue
            value = kw.value
            if (
                isinstance(value, Constant)
                and isinstance(value.value, str)
                and value.value.strip()
            ):
                return True
    return False


def _collect_functions(
    tree: AST,
) -> dict[str, FunctionDef | AsyncFunctionDef]:
    functions: dict[str, FunctionDef | AsyncFunctionDef] = {}
    for node in walk(tree):
        if isinstance(node, FunctionDef | AsyncFunctionDef):
            functions[node.name] = node
    return functions


def _tool_function_name(call: Call) -> str | None:
    target: AST | None = call.args[0] if call.args else None
    if target is None:
        for kw in call.keywords:
            if kw.arg == "function":
                target = kw.value
                break
    return target.id if isinstance(target, Name) else None


def _description_finding(path: Path, call: Call) -> LintFinding | None:
    description: AST | None = None
    for kw in call.keywords:
        if kw.arg == "description":
            description = kw.value
            break
    if description is None:
        return LintFinding(
            rule="PL022",
            path=path,
            line=call.lineno,
            col=call.col_offset + 1,
            message="Tool(...) must provide a non-empty description=.",
            hint=(
                "Pass description='...' explaining what the tool does so the model "
                "knows when to call it."
            ),
        )
    if isinstance(description, Constant):
        value = description.value
        if not isinstance(value, str) or not value.strip():
            return LintFinding(
                rule="PL022",
                path=path,
                line=call.lineno,
                col=call.col_offset + 1,
                message="Tool(...) description= must be a non-empty string.",
                hint="Replace the empty description with an explanatory string.",
            )
    return None


def _parameter_finding(
    path: Path,
    function: FunctionDef | AsyncFunctionDef,
) -> LintFinding | None:
    arguments = [
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ]
    if function.args.vararg is not None:
        arguments.append(function.args.vararg)
    if function.args.kwarg is not None:
        arguments.append(function.args.kwarg)
    for argument in arguments:
        if argument.arg in {"self", "cls"}:
            continue
        annotation = argument.annotation
        if annotation is None:
            return LintFinding(
                rule="PL022",
                path=path,
                line=function.lineno,
                col=function.col_offset + 1,
                message=(
                    f"Tool parameter '{argument.arg}' must use "
                    "Annotated[T, Field(description=...)]."
                ),
                hint=(
                    "Annotate every tool parameter with Field(description='...') so "
                    "the model receives a schema with per-parameter descriptions."
                ),
            )
        if _is_run_context(annotation):
            continue
        if not _has_field_description(annotation):
            return LintFinding(
                rule="PL022",
                path=path,
                line=argument.lineno,
                col=argument.col_offset + 1,
                message=(
                    f"Tool parameter '{argument.arg}' must use "
                    "Annotated[T, Field(description=...)] with a non-empty "
                    "description."
                ),
                hint=(
                    "Wrap the annotation in Annotated[T, Field(description='...')] "
                    "and provide a non-empty string description."
                ),
            )
    return None


def check_tool_descriptions(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        names, module_prefixes = _collect_tool_names(tree)
        if not names and not module_prefixes:
            continue
        functions = _collect_functions(tree)
        for node in walk(tree):
            if not _is_tool_call(node, names, module_prefixes):
                continue
            description_finding = _description_finding(path, node)
            if description_finding is not None:
                findings.append(description_finding)
            function_name = _tool_function_name(node)
            if function_name is None:
                continue
            function = functions.get(function_name)
            if function is None:
                continue
            parameter_finding = _parameter_finding(path, function)
            if parameter_finding is not None:
                findings.append(parameter_finding)
    return findings
