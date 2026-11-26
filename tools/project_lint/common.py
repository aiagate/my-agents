from ast import (
    AST,
    AsyncFunctionDef,
    Attribute,
    Call,
    ClassDef,
    Constant,
    FunctionDef,
    Import,
    ImportFrom,
    Name,
    Subscript,
    Tuple,
    arg,
    parse,
)
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PACKAGE_DIR = PROJECT_ROOT / "src" / "my_agents"
TESTS_DIR = PROJECT_ROOT / "tests"
TOOLS_DIR = PROJECT_ROOT / "tools"


@dataclass(frozen=True, slots=True)
class LintFinding:
    rule: str
    path: Path
    line: int
    col: int
    message: str
    hint: str

    def format(self, root: Path = PROJECT_ROOT) -> str:
        rel = (
            self.path.relative_to(root) if self.path.is_relative_to(root) else self.path
        )
        return (
            f"{self.rule} {rel}:{self.line}:{self.col} {self.message}\n"
            f"  hint: {self.hint}"
        )


@dataclass(frozen=True, slots=True)
class LintRule:
    code: str
    name: str
    check_root: Callable[[Path], list[LintFinding]]


def iter_python_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


@cache
def parse_file(path: Path) -> AST:
    return parse(path.read_text(encoding="utf-8"), filename=str(path))


def node_name(node: AST | None) -> str | None:
    if isinstance(node, Name):
        return node.id
    if isinstance(node, Attribute):
        return node.attr
    return None


def dotted_name(node: AST | None) -> str | None:
    if isinstance(node, Name):
        return node.id
    if isinstance(node, Attribute):
        parent = dotted_name(node.value)
        if parent is not None:
            return f"{parent}.{node.attr}"
    return None


def call_name(node: AST) -> str | None:
    return node_name(node.func) if isinstance(node, Call) else None


def keyword_value(call: Call, keyword: str) -> AST | None:
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def constant_is(node: AST | None, value: object) -> bool:
    return isinstance(node, Constant) and node.value is value


def subscript_arguments(node: Subscript) -> list[AST]:
    return list(node.slice.elts) if isinstance(node.slice, Tuple) else [node.slice]


def function_arguments(
    function: FunctionDef | AsyncFunctionDef,
) -> list[arg]:
    arguments = [
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ]
    if function.args.vararg is not None:
        arguments.append(function.args.vararg)
    if function.args.kwarg is not None:
        arguments.append(function.args.kwarg)
    return arguments


def decorator_target(decorator: AST) -> AST:
    return decorator.func if isinstance(decorator, Call) else decorator


def has_decorator_name(
    stmt: FunctionDef | AsyncFunctionDef,
    names: frozenset[str],
) -> bool:
    return any(
        node_name(decorator_target(decorator)) in names
        for decorator in stmt.decorator_list
    )


def is_dataclass_decorator(decorator: AST) -> bool:
    return node_name(decorator_target(decorator)) == "dataclass"


def has_dataclass_decorator(node: ClassDef) -> bool:
    return any(is_dataclass_decorator(decorator) for decorator in node.decorator_list)


def dataclass_keyword(node: ClassDef, keyword: str) -> AST | None:
    for decorator in node.decorator_list:
        if isinstance(decorator, Call) and is_dataclass_decorator(decorator):
            value = keyword_value(decorator, keyword)
            if value is not None:
                return value
    return None


def base_class_name(base: AST) -> str | None:
    return node_name(base.value if isinstance(base, Subscript) else base)


def base_class_names(node: ClassDef) -> list[str]:
    return [name for base in node.bases if (name := base_class_name(base)) is not None]


def is_protocol_class(node: ClassDef) -> bool:
    return any(name == "Protocol" for name in base_class_names(node))


def annotation_is_non_init_marker(annotation: AST) -> bool:
    return node_name(
        annotation.value if isinstance(annotation, Subscript) else annotation
    ) in {
        "ClassVar",
        "InitVar",
    }


def find_line_number(content: str, token: str) -> int:
    for idx, line in enumerate(content.splitlines(), start=1):
        if token in line:
            return idx
    return 1


def as_table(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    table = {key: item for key, item in value.items() if isinstance(key, str)}
    return table if len(table) == len(value) else None


def resolve_import_targets(
    node: Import | ImportFrom,
    path: Path,
    src_dir: Path,
) -> list[str]:
    if isinstance(node, Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        return [node.module] if node.module else []
    rel_parts = list(path.relative_to(src_dir).with_suffix("").parts)
    # Package __init__.py maps to the directory package, matching module sibling level.
    package_parts = ["my_agents", *rel_parts[:-1]]
    depth = max(len(package_parts) - (node.level - 1), 0)
    prefix = package_parts[:depth]
    if node.module:
        return [".".join([*prefix, node.module])]
    return [".".join([*prefix, alias.name]) for alias in node.names]


def path_rule(
    code: str,
    name: str,
    parts: tuple[str, ...],
    check: Callable[[Path], list[LintFinding]],
) -> LintRule:
    def _run(root: Path) -> list[LintFinding]:
        return check(root.joinpath(*parts))

    return LintRule(code=code, name=name, check_root=_run)


def dirs_rule(
    code: str,
    name: str,
    parts: tuple[tuple[str, ...], ...],
    check: Callable[[Sequence[Path]], list[LintFinding]],
) -> LintRule:
    def _run(root: Path) -> list[LintFinding]:
        return check(tuple(root.joinpath(*part) for part in parts))

    return LintRule(code=code, name=name, check_root=_run)
