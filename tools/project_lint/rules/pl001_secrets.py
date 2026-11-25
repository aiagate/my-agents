from ast import (
    AST,
    AnnAssign,
    Assign,
    AsyncFunctionDef,
    Attribute,
    ClassDef,
    FunctionDef,
    Name,
    arg,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    annotation_is_non_init_marker,
    base_class_names,
    has_dataclass_decorator,
    has_decorator_name,
    iter_python_files,
    parse_file,
)

_SECRET_SEGMENTS = frozenset(
    {"secret", "password", "passwd", "credential", "credentials"}
)
_SECRET_COMPOUND_SUFFIXES = (
    "api_key",
    "access_key",
    "private_key",
    "signing_key",
    "auth_key",
    "secret_key",
)
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
    parts = lowered.split("_")
    if any(part in _SECRET_SEGMENTS for part in parts):
        return True
    if lowered == "token" or lowered.endswith("_token"):
        return True
    return any(
        lowered == suffix or lowered.endswith(f"_{suffix}")
        for suffix in _SECRET_COMPOUND_SUFFIXES
    )


def _annotation_uses_plain_str(annotation: AST | None) -> bool:
    if annotation is None:
        return False
    return any(isinstance(node, Name) and node.id == "str" for node in walk(annotation))


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


def check_no_plain_str_secrets(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    findings: list[LintFinding] = []
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
