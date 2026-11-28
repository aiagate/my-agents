from ast import (
    AST,
    AnnAssign,
    AsyncFunctionDef,
    Attribute,
    ClassDef,
    FunctionDef,
    Module,
    Name,
    Subscript,
    Tuple,
    TypeAlias,
    arg,
    expr,
    iter_child_nodes,
    stmt,
)
from collections.abc import Iterator
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    function_arguments,
    iter_python_files,
    parse_file,
)

type _AnnotationSite = tuple[str, arg | expr | stmt, expr | None]


def _is_private_name(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _private_type_names(annotation: AST) -> Iterator[str]:
    match annotation:
        case Name(id=name):
            if _is_private_name(name):
                yield name
        case Attribute(value=value, attr=name):
            if _is_private_name(name):
                yield name
            yield from _private_type_names(value)
        case Subscript(
            value=Name(id="Annotated") | Attribute(attr="Annotated"), slice=wrapped
        ):
            if isinstance(wrapped, Tuple) and wrapped.elts:
                wrapped = wrapped.elts[0]
            yield from _private_type_names(wrapped)
        case Subscript(value=Name(id="Literal") | Attribute(attr="Literal")):
            return
        case _:
            for child in iter_child_nodes(annotation):
                yield from _private_type_names(child)


def _function_annotations(
    function: FunctionDef | AsyncFunctionDef, prefix: str
) -> Iterator[_AnnotationSite]:
    symbol = f"{prefix}{function.name}"
    for argument in function_arguments(function):
        if prefix and argument.arg in {"self", "cls"}:
            continue
        yield f"{symbol}({argument.arg})", argument, argument.annotation
    yield f"{symbol}() return", function, function.returns


def _public_annotations(
    body: list[stmt], prefix: str = ""
) -> Iterator[_AnnotationSite]:
    for node in body:
        match node:
            case FunctionDef(name=name) | AsyncFunctionDef(name=name) if (
                not _is_private_name(name)
            ):
                yield from _function_annotations(node, prefix)
            case ClassDef(name=name) if not _is_private_name(name):
                symbol = f"{prefix}{name}"
                for base in node.bases:
                    yield f"{symbol} base", base, base
                yield from _public_annotations(node.body, f"{symbol}.")
            case TypeAlias(name=Name(id=name)) if not prefix and not _is_private_name(
                name
            ):
                yield name, node, node.value
            case AnnAssign(target=Name(id=name)) if not _is_private_name(name):
                yield f"{prefix}{name}", node, node.annotation
            case _:
                continue


def check_no_private_type_in_public_api(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        rel_parts = path.relative_to(src_dir).with_suffix("").parts
        if any(_is_private_name(part) for part in rel_parts):
            continue
        tree = parse_file(path)
        if not isinstance(tree, Module):
            continue
        for symbol, target, annotation in _public_annotations(tree.body):
            if annotation is None:
                continue
            private_names = sorted(set(_private_type_names(annotation)))
            if not private_names:
                continue
            findings.append(
                LintFinding(
                    rule="PL023",
                    path=path,
                    line=target.lineno,
                    col=target.col_offset + 1,
                    message=(
                        f"Public API '{symbol}' exposes private type(s) "
                        f"{', '.join(private_names)}."
                    ),
                    hint=(
                        "Expose a public type or Protocol, or keep the private type "
                        "internal by constructing it in a public classmethod factory."
                    ),
                )
            )
    return findings
