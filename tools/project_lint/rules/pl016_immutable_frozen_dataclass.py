from ast import (
    AST,
    AnnAssign,
    Attribute,
    BinOp,
    Call,
    ClassDef,
    Dict,
    List,
    Name,
    Set,
    Subscript,
    Tuple,
    expr,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    annotation_is_non_init_marker,
    constant_is,
    dataclass_keyword,
    iter_python_files,
    node_name,
    parse_file,
)

_FLEXIBLE_COLLECTION_TYPES = frozenset(
    {"Collection", "Iterable", "Mapping", "Sequence"}
)
_WRAPPER_CALLS = frozenset({"MappingProxyType", "frozenset", "tuple"})
_MUTABLE_CONSTRUCTORS = frozenset({"dict", "list", "set"})
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
        "MutableSequence",
        "MutableMapping",
        "MutableSet",
    }
)


def _is_frozen_dataclass(node: ClassDef) -> bool:
    return constant_is(dataclass_keyword(node, "frozen"), True)


def _find_disallowed_collection_types(annotation: AST) -> list[str]:
    if isinstance(annotation, Subscript):
        outer_name = node_name(annotation.value)
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
    leaf_name = node_name(annotation)
    if leaf_name is not None and leaf_name in _DISALLOWED_COLLECTION_TYPES:
        return [leaf_name]
    return []


def _annotation_has_flexible_collection(annotation: AST) -> bool:
    return any(
        isinstance(node, Subscript)
        and node_name(node.value) in _FLEXIBLE_COLLECTION_TYPES
        for node in walk(annotation)
    )


def _annotation_has_mutable_collection(annotation: AST) -> bool:
    return bool(_find_disallowed_collection_types(annotation))


def _frozen_dataclass_fields(
    src_dir: Path,
) -> dict[str, list[tuple[str, bool]]]:
    classes: dict[str, list[tuple[str, bool]]] = {}
    for path in iter_python_files(src_dir):
        for node in walk(parse_file(path)):
            if not isinstance(node, ClassDef) or not _is_frozen_dataclass(node):
                continue
            classes[node.name] = [
                (
                    stmt.target.id,
                    _annotation_has_flexible_collection(stmt.annotation),
                )
                for stmt in node.body
                if isinstance(stmt, AnnAssign) and isinstance(stmt.target, Name)
            ]
    return classes


def _mutable_annotated_names(tree: AST) -> set[str]:
    names: set[str] = set()
    for node in walk(tree):
        if (
            isinstance(node, AnnAssign)
            and isinstance(node.target, Name)
            and _annotation_has_mutable_collection(node.annotation)
        ):
            names.add(node.target.id)
    return names


def _is_wrapped(argument: AST) -> bool:
    if isinstance(argument, Call):
        if isinstance(argument.func, Name):
            return argument.func.id in _WRAPPER_CALLS
        if isinstance(argument.func, Attribute):
            return argument.func.attr in _WRAPPER_CALLS
    return isinstance(argument, Tuple)


def _is_unwrapped_mutable(argument: AST, mutable_names: set[str]) -> bool:
    if _is_wrapped(argument):
        return False
    if isinstance(argument, Dict | List | Set):
        return True
    if isinstance(argument, Call):
        if isinstance(argument.func, Name):
            return argument.func.id in _MUTABLE_CONSTRUCTORS
        if isinstance(argument.func, Attribute):
            return argument.func.attr in _MUTABLE_CONSTRUCTORS
    if isinstance(argument, Name):
        return argument.id in mutable_names
    return isinstance(argument, Attribute) and argument.attr in mutable_names


def _constructor_findings(
    tree: AST,
    path: Path,
    classes: dict[str, list[tuple[str, bool]]],
) -> list[LintFinding]:
    mutable_names = _mutable_annotated_names(tree)
    findings: list[LintFinding] = []
    for node in walk(tree):
        if not isinstance(node, Call) or not isinstance(node.func, Name):
            continue
        fields = classes.get(node.func.id)
        if fields is None:
            continue
        candidates: list[tuple[int, expr]] = []
        for index, argument in enumerate(node.args):
            if index < len(fields):
                candidates.append((index, argument))
        field_names = [name for name, _ in fields]
        for keyword in node.keywords:
            if keyword.arg is not None and keyword.arg in field_names:
                candidates.append((field_names.index(keyword.arg), keyword.value))
        for index, argument in candidates:
            field_name, flexible = fields[index]
            if not flexible:
                continue
            if not _is_unwrapped_mutable(argument, mutable_names):
                continue
            findings.append(
                LintFinding(
                    rule="PL016",
                    path=path,
                    line=argument.lineno,
                    col=argument.col_offset + 1,
                    message=(
                        f"Frozen dataclass '{node.func.id}' field '{field_name}' "
                        "receives a mutable collection value directly."
                    ),
                    hint=(
                        "Wrap the value with MappingProxyType(...), tuple(...), or "
                        "frozenset(...) before constructing a frozen dataclass."
                    ),
                )
            )
    return findings


def check_immutable_frozen_dataclass_fields(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    classes = _frozen_dataclass_fields(src_dir)
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        findings.extend(_constructor_findings(tree, path, classes))
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
