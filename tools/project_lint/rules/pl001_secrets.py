from ast import (
    AST,
    AnnAssign,
    Assign,
    AsyncFunctionDef,
    Attribute,
    AugAssign,
    BinOp,
    Call,
    ClassDef,
    Dict,
    FunctionDef,
    IfExp,
    List,
    Name,
    Return,
    Set,
    Subscript,
    Tuple,
    arg,
    iter_child_nodes,
    walk,
)
from collections.abc import Sequence
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    annotation_is_non_init_marker,
    base_class_names,
    function_arguments,
    has_dataclass_decorator,
    has_decorator_name,
    iter_python_files,
    node_name,
    parse_file,
)

_SECRET_SEGMENTS = frozenset(
    {"secret", "password", "passwd", "credential", "credentials"}
)
_PLURAL_SECRET_SEGMENTS = {
    "secrets": "secret",
    "passwords": "password",
    "passwds": "passwd",
    "tokens": "token",
    "credentials": "credential",
    "keys": "key",
}
_SECRET_COMPOUND_SUFFIXES = (
    "api_key",
    "access_key",
    "private_key",
    "signing_key",
    "auth_key",
    "secret_key",
)
_STR_CONTAINER_TYPES = frozenset(
    {
        "Collection",
        "FrozenSet",
        "Iterable",
        "Iterator",
        "List",
        "Mapping",
        "MutableMapping",
        "MutableSequence",
        "MutableSet",
        "Sequence",
        "Set",
        "Tuple",
        "dict",
        "frozenset",
        "list",
        "set",
        "tuple",
    }
)
# reason=Non-secret identifiers containing token/key substrings (e.g. token counts, header names); expires=2027-11-27; approved-by=human
_NON_SECRET_HINTS = (
    "token_count",
    "tokens",
    "max_tokens",
    "token_limit",
    "tokenizer",
    "key_name",
    "setting_name",
    "env_key",
    "header_name",
)


def _is_secret_identifier(name: str) -> bool:
    lowered = name.lstrip("_").lower()
    if not lowered:
        return False
    if any(hint in lowered for hint in _NON_SECRET_HINTS):
        return False
    raw_parts = lowered.split("_")
    parts = [_PLURAL_SECRET_SEGMENTS.get(part, part) for part in raw_parts]
    if any(part in _SECRET_SEGMENTS for part in parts):
        return True
    normalized = "_".join(parts)
    if normalized == "token" or normalized.endswith("_token"):
        return True
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _SECRET_COMPOUND_SUFFIXES
    )


def _annotation_uses_plain_str(annotation: AST | None) -> bool:
    if annotation is None:
        return False
    return any(isinstance(node, Name) and node.id == "str" for node in walk(annotation))


def _annotation_has_str_container(annotation: AST | None) -> bool:
    if annotation is None:
        return False
    for node in walk(annotation):
        if not isinstance(node, Subscript):
            continue
        outer = node_name(node.value)
        if outer not in _STR_CONTAINER_TYPES:
            continue
        if any(
            isinstance(child, Name) and child.id == "str" for child in walk(node.slice)
        ):
            return True
    return False


def _inherits_secret_vo(node: ClassDef) -> bool:
    return any("secret" in name.lower() for name in base_class_names(node))


def _is_secret_vo_class(node: ClassDef) -> bool:
    return "secret" in node.name.lower() or _inherits_secret_vo(node)


def _find_secret_public_attributes(node: ClassDef) -> list[tuple[str, int, int]]:
    exposed: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for stmt in node.body:
        if (
            isinstance(stmt, AnnAssign)
            and isinstance(stmt.target, Name)
            and not stmt.target.id.startswith("_")
            and not annotation_is_non_init_marker(stmt.annotation)
            and stmt.target.id not in seen
        ):
            seen.add(stmt.target.id)
            exposed.append((stmt.target.id, stmt.lineno, stmt.col_offset + 1))
        elif (
            isinstance(stmt, FunctionDef | AsyncFunctionDef)
            and not stmt.name.startswith("_")
            and has_decorator_name(stmt, frozenset({"property"}))
            and stmt.name not in seen
        ):
            seen.add(stmt.name)
            exposed.append((stmt.name, stmt.lineno, stmt.col_offset + 1))
        elif isinstance(stmt, FunctionDef) and stmt.name == "__init__":
            for child in walk(stmt):
                if isinstance(child, Assign):
                    for target in child.targets:
                        if (
                            isinstance(target, Attribute)
                            and isinstance(target.value, Name)
                            and target.value.id == "self"
                            and not target.attr.startswith("_")
                            and target.attr not in seen
                        ):
                            seen.add(target.attr)
                            exposed.append(
                                (target.attr, target.lineno, target.col_offset + 1)
                            )
                elif (
                    isinstance(child, AnnAssign)
                    and isinstance(child.target, Attribute)
                    and isinstance(child.target.value, Name)
                    and child.target.value.id == "self"
                    and not child.target.attr.startswith("_")
                    and child.target.attr not in seen
                ):
                    seen.add(child.target.attr)
                    exposed.append(
                        (
                            child.target.attr,
                            child.target.lineno,
                            child.target.col_offset + 1,
                        )
                    )
    return exposed


def _missing_secret_vo_methods(node: ClassDef) -> list[str]:
    defined = {stmt.name for stmt in node.body if isinstance(stmt, FunctionDef)}
    return [
        name
        for name in ("__repr__", "__str__", "__eq__", "__hash__")
        if name not in defined
    ]


def _parameter_containers(
    function: FunctionDef | AsyncFunctionDef,
) -> list[tuple[str, bool]]:
    return [
        (argument.arg, _annotation_has_str_container(argument.annotation))
        for argument in function_arguments(function)
    ]


def _collect_repository_callables(
    src_dir: Path,
) -> dict[str, list[tuple[str, bool]]]:
    callables: dict[str, list[tuple[str, bool]]] = {}
    for path in iter_python_files(src_dir):
        for node in walk(parse_file(path)):
            if isinstance(node, FunctionDef | AsyncFunctionDef):
                callables.setdefault(node.name, _parameter_containers(node))
            elif isinstance(node, ClassDef):
                initializer = next(
                    (
                        stmt
                        for stmt in node.body
                        if isinstance(stmt, FunctionDef) and stmt.name == "__init__"
                    ),
                    None,
                )
                if initializer is not None:
                    callables.setdefault(node.name, _parameter_containers(initializer))
                elif has_dataclass_decorator(node):
                    callables.setdefault(
                        node.name,
                        [
                            (
                                stmt.target.id,
                                _annotation_has_str_container(stmt.annotation),
                            )
                            for stmt in node.body
                            if isinstance(stmt, AnnAssign)
                            and isinstance(stmt.target, Name)
                        ],
                    )
    return callables


def _call_name(node: Call) -> str | None:
    return node_name(node.func)


def _is_self_attribute(node: AST, attr: str) -> bool:
    return (
        isinstance(node, Attribute)
        and node.attr == attr
        and isinstance(node.value, Name)
        and node.value.id == "self"
    )


def _expr_is_tainted(
    node: AST | None,
    tainted: set[str],
    repository_callables: dict[str, list[tuple[str, bool]]],
    tainted_returns: set[str],
) -> bool:
    if node is None:
        return False
    if isinstance(node, Call):
        if isinstance(node.func, Attribute) and node.func.attr == "get_secret_value":
            return True
        callee = _call_name(node)
        if callee is not None and callee in tainted_returns:
            return True
        if callee is not None and callee in repository_callables:
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            return any(
                _expr_is_tainted(
                    argument, tainted, repository_callables, tainted_returns
                )
                for argument in arguments
            )
        return False
    if isinstance(node, Name):
        return node.id in tainted
    if isinstance(node, Attribute):
        return f"self.{node.attr}" in tainted
    if isinstance(node, Dict):
        values = [*node.keys, *node.values]
        return any(
            _expr_is_tainted(value, tainted, repository_callables, tainted_returns)
            for value in values
            if value is not None
        )
    if isinstance(node, List | Set | Tuple):
        return any(
            _expr_is_tainted(elt, tainted, repository_callables, tainted_returns)
            for elt in node.elts
        )
    if isinstance(node, BinOp):
        return _expr_is_tainted(
            node.left, tainted, repository_callables, tainted_returns
        ) or _expr_is_tainted(
            node.right, tainted, repository_callables, tainted_returns
        )
    if isinstance(node, IfExp):
        return _expr_is_tainted(
            node.body, tainted, repository_callables, tainted_returns
        ) or _expr_is_tainted(
            node.orelse, tainted, repository_callables, tainted_returns
        )
    if isinstance(node, Subscript):
        return _expr_is_tainted(
            node.value, tainted, repository_callables, tainted_returns
        ) or _expr_is_tainted(
            node.slice, tainted, repository_callables, tainted_returns
        )
    return any(
        _expr_is_tainted(child, tainted, repository_callables, tainted_returns)
        for child in iter_child_nodes(node)
        if isinstance(child, AST)
    )


def _add_target_names(target: AST, tainted: set[str]) -> None:
    if isinstance(target, Name):
        tainted.add(target.id)
    elif isinstance(target, Attribute):
        if isinstance(target.value, Name) and target.value.id == "self":
            tainted.add(f"self.{target.attr}")
    elif isinstance(target, List | Tuple):
        for elt in target.elts:
            _add_target_names(elt, tainted)


def _tainted_returns(
    tree: AST, tainted: set[str], callables: dict[str, list]
) -> set[str]:
    returns: set[str] = set()
    for node in walk(tree):
        if not isinstance(node, FunctionDef | AsyncFunctionDef):
            continue
        if any(
            isinstance(child, Return)
            and _expr_is_tainted(child.value, tainted, callables, returns)
            for child in walk(node)
        ):
            returns.add(node.name)
    return returns


def _compute_taint(
    tree: AST,
    callables: dict[str, list[tuple[str, bool]]],
) -> tuple[set[str], set[str]]:
    tainted: set[str] = set()
    tainted_returns: set[str] = set()
    changed = True
    while changed:
        changed = False
        returns = _tainted_returns(tree, tainted, callables)
        if returns - tainted_returns:
            tainted_returns |= returns
            changed = True
        for node in walk(tree):
            parts = _assignment_parts(node)
            if parts is None:
                continue
            value, targets = parts
            if not _expr_is_tainted(value, tainted, callables, tainted_returns):
                continue
            before = len(tainted)
            for target in targets:
                _add_target_names(target, tainted)
            if len(tainted) != before:
                changed = True
    return tainted, tainted_returns


def _assignment_parts(node: AST) -> tuple[AST, Sequence[AST]] | None:
    if isinstance(node, Assign):
        return node.value, node.targets
    if isinstance(node, AnnAssign) and node.value is not None:
        return node.value, [node.target]
    if isinstance(node, AugAssign):
        return node.value, [node.target]
    return None


def _attribute_hold_findings(
    tree: AST,
    path: Path,
    callables: dict[str, list[tuple[str, bool]]],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    tainted, tainted_returns = _compute_taint(tree, callables)
    for node in walk(tree):
        parts = _assignment_parts(node)
        if parts is None:
            continue
        value, targets = parts
        if not _expr_is_tainted(value, tainted, callables, tainted_returns):
            continue
        for target in targets:
            if isinstance(target, Attribute) and _is_self_attribute(
                target, target.attr
            ):
                findings.append(
                    LintFinding(
                        rule="PL001",
                        path=path,
                        line=target.lineno,
                        col=target.col_offset + 1,
                        message=(
                            "Raw secret from 'get_secret_value()' is retained on "
                            f"'self.{target.attr}' as a plain value."
                        ),
                        hint=(
                            "Keep the SecretStr/SecretValue object and call "
                            "'get_secret_value()' only at the moment an external "
                            "library requires the raw string; do not store or "
                            "propagate the raw value."
                        ),
                    )
                )
    return findings


def _repository_argument_findings(
    tree: AST,
    path: Path,
    callables: dict[str, list[tuple[str, bool]]],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    tainted, tainted_returns = _compute_taint(tree, callables)
    for node in walk(tree):
        if not isinstance(node, Call):
            continue
        callee = _call_name(node)
        if callee is None or callee not in callables:
            continue
        parameters = callables[callee]
        parameter_names = [name for name, _ in parameters]
        candidates: list[tuple[int, AST]] = []
        for index, argument in enumerate(node.args):
            if index < len(parameters):
                candidates.append((index, argument))
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            if keyword.arg in parameter_names:
                candidates.append((parameter_names.index(keyword.arg), keyword.value))
        for index, argument in candidates:
            if not parameters[index][1]:
                continue
            if not _expr_is_tainted(argument, tainted, callables, tainted_returns):
                continue
            findings.append(
                LintFinding(
                    rule="PL001",
                    path=path,
                    line=node.lineno,
                    col=node.col_offset + 1,
                    message=(
                        f"Raw secret is passed as a plain 'str' container to "
                        f"repository callable '{callee}' parameter "
                        f"'{parameters[index][0]}'."
                    ),
                    hint=(
                        "Pass SecretStr/SecretValue values or unwrap them only at "
                        "the external library boundary; repository callables "
                        "should accept Secret containers."
                    ),
                )
            )
    return findings


def check_no_plain_str_secrets(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    findings: list[LintFinding] = []
    callables = _collect_repository_callables(src_dir)
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        exempt_nodes: set[int] = set()
        for node in walk(tree):
            if isinstance(node, ClassDef) and _is_secret_vo_class(node):
                for child in walk(node):
                    exempt_nodes.add(id(child))
                if not _inherits_secret_vo(node):
                    if has_dataclass_decorator(node):
                        findings.append(
                            LintFinding(
                                rule="PL001",
                                path=path,
                                line=node.lineno,
                                col=node.col_offset + 1,
                                message=(
                                    f"Secret Value Object '{node.name}' must not be "
                                    "a @dataclass."
                                ),
                                hint=(
                                    "Implement Secret Value Objects as plain classes "
                                    "with '__slots__' (not @dataclass) so "
                                    "dataclasses.asdict() cannot recursively leak "
                                    "secret values; accept a positional-only value in "
                                    "'__init__(self, value: str, /)', store it in a "
                                    "private '_'-prefixed attribute, define "
                                    "'__repr__', '__str__', '__eq__', '__hash__', and "
                                    "expose it only via 'get_secret_value()'."
                                ),
                            )
                        )
                    for attr_name, line, col in _find_secret_public_attributes(node):
                        findings.append(
                            LintFinding(
                                rule="PL001",
                                path=path,
                                line=line,
                                col=col,
                                message=(
                                    f"Secret Value Object '{node.name}' must not "
                                    f"expose public attribute '{attr_name}'."
                                ),
                                hint=(
                                    "Store the raw secret in a private '_'-prefixed "
                                    "attribute (e.g. 'self._value') and expose it "
                                    "only through 'get_secret_value()'."
                                ),
                            )
                        )
                    missing = _missing_secret_vo_methods(node)
                    if missing:
                        findings.append(
                            LintFinding(
                                rule="PL001",
                                path=path,
                                line=node.lineno,
                                col=node.col_offset + 1,
                                message=(
                                    f"Secret Value Object '{node.name}' must define "
                                    f"{', '.join(missing)}."
                                ),
                                hint=(
                                    "Define '__repr__', '__str__', '__eq__', and "
                                    "'__hash__' on Secret Value Objects so secrets "
                                    "stay masked, value-comparable, and hashable."
                                ),
                            )
                        )

        findings.extend(_attribute_hold_findings(tree, path, callables))
        findings.extend(_repository_argument_findings(tree, path, callables))

        for node in walk(tree):
            if id(node) in exempt_nodes:
                continue
            if (
                isinstance(node, AnnAssign)
                and isinstance(node.target, Name)
                and _is_secret_identifier(node.target.id)
                and _annotation_uses_plain_str(node.annotation)
            ):
                findings.append(
                    LintFinding(
                        rule="PL001",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message=(
                            f"Secret-like field '{node.target.id}' is typed as "
                            "plain 'str'."
                        ),
                        hint=(
                            "Define or use a Secret Value Object (e.g. SecretStr / "
                            "SecretValue subclass that masks __repr__ and __str__) "
                            "instead of plain 'str'."
                        ),
                    )
                )
            elif (
                isinstance(node, arg)
                and _is_secret_identifier(node.arg)
                and _annotation_uses_plain_str(node.annotation)
            ):
                findings.append(
                    LintFinding(
                        rule="PL001",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message=(
                            f"Secret-like parameter '{node.arg}' is typed as "
                            "plain 'str'."
                        ),
                        hint=(
                            "Accept a Secret Value Object (e.g. SecretStr / "
                            "SecretValue subclass) instead of plain 'str'."
                        ),
                    )
                )
    return findings
