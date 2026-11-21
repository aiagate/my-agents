from ast import (
    AST,
    AsyncFunctionDef,
    Attribute,
    Call,
    FunctionDef,
    Import,
    ImportFrom,
    Name,
    Subscript,
    parse,
)
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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


def parse_file(path: Path) -> AST:
    return parse(path.read_text(encoding="utf-8"), filename=str(path))


def has_decorator_name(
    stmt: FunctionDef | AsyncFunctionDef,
    names: frozenset[str],
) -> bool:
    for decorator in stmt.decorator_list:
        target = decorator.func if isinstance(decorator, Call) else decorator
        if isinstance(target, Name) and target.id in names:
            return True
        if isinstance(target, Attribute) and target.attr in names:
            return True
    return False


def annotation_is_non_init_marker(annotation: AST) -> bool:
    target = annotation.value if isinstance(annotation, Subscript) else annotation
    if isinstance(target, Name):
        return target.id in {"ClassVar", "InitVar"}
    if isinstance(target, Attribute):
        return target.attr in {"ClassVar", "InitVar"}
    return False


def find_line_number(content: str, token: str) -> int:
    for idx, line in enumerate(content.splitlines(), start=1):
        if token in line:
            return idx
    return 1


def as_table(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


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
