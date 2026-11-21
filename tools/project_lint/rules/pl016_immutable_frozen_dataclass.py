from ast import (
    AST,
    AnnAssign,
    Attribute,
    BinOp,
    Call,
    ClassDef,
    Constant,
    Name,
    Subscript,
    Tuple,
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

_DISALLOWED_COLLECTION_TYPES = frozenset(
    {
        "list",
        "dict",
        "set",
        "bytearray",
        "deque",
        "defaultdict",
        "Counter",
        "OrderedDict",
        "Sequence",
        "MutableSequence",
        "Mapping",
        "MutableMapping",
        "Set",
        "MutableSet",
        "Collection",
        "Iterable",
    }
)


def _is_frozen_dataclass(node: ClassDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, Call) else decorator
        is_dataclass = (isinstance(target, Name) and target.id == "dataclass") or (
            isinstance(target, Attribute) and target.attr == "dataclass"
        )
        if not is_dataclass or not isinstance(decorator, Call):
            continue
        for kw in decorator.keywords:
            if (
                kw.arg == "frozen"
                and isinstance(kw.value, Constant)
                and kw.value.value is True
            ):
                return True
    return False


def _type_node_name(node: AST) -> str | None:
    if isinstance(node, Name):
        return node.id
    if isinstance(node, Attribute):
        return node.attr
    return None


def _find_disallowed_collection_types(annotation: AST) -> list[str]:
    if isinstance(annotation, Subscript):
        outer_name = _type_node_name(annotation.value)
        # Parameter/return types of callbacks do not affect field immutability.
        if outer_name == "Callable":
            return []
        found: list[str] = []
        if outer_name is not None and outer_name in _DISALLOWED_COLLECTION_TYPES:
            found.append(outer_name)
        found.extend(_find_disallowed_collection_types(annotation.slice))
        return found
    if isinstance(annotation, BinOp):
        return [
            *_find_disallowed_collection_types(annotation.left),
            *_find_disallowed_collection_types(annotation.right),
        ]
    if isinstance(annotation, Tuple):
        found = []
        for elt in annotation.elts:
            found.extend(_find_disallowed_collection_types(elt))
        return found
    leaf_name = _type_node_name(annotation)
    if leaf_name is not None and leaf_name in _DISALLOWED_COLLECTION_TYPES:
        return [leaf_name]
    return []


def check_immutable_frozen_dataclass_fields(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, ClassDef) or not _is_frozen_dataclass(node):
                continue
            for stmt in node.body:
                if not isinstance(stmt, AnnAssign) or not isinstance(stmt.target, Name):
                    continue
                if annotation_is_non_init_marker(stmt.annotation):
                    continue
                disallowed = _find_disallowed_collection_types(stmt.annotation)
                if not disallowed:
                    continue
                types_label = ", ".join(dict.fromkeys(disallowed))
                findings.append(
                    LintFinding(
                        rule="PL016",
                        path=path,
                        line=stmt.lineno,
                        col=stmt.col_offset + 1,
                        message=(
                            f"Frozen dataclass '{node.name}' field '{stmt.target.id}' "
                            f"uses mutable or mutable-accepting type '{types_label}'."
                        ),
                        hint=(
                            "Use truly immutable container types such as "
                            "'tuple[T, ...]' or 'frozenset[T]' on frozen dataclasses."
                        ),
                    )
                )
    return findings
