from ast import (
    AST,
    Assign,
    Attribute,
    AugAssign,
    Call,
    Delete,
    Import,
    ImportFrom,
    Name,
    Subscript,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)

_MUTATING_METHODS = frozenset({"setdefault", "update", "pop", "popitem", "clear"})
_OS_ENV_MUTATORS = frozenset({"putenv", "unsetenv"})


def _collect_env_aliases(
    tree: AST,
) -> tuple[set[str], set[str], set[str]]:
    os_aliases: set[str] = set()
    environ_aliases: set[str] = set()
    putenv_aliases: set[str] = set()
    for node in walk(tree):
        if isinstance(node, Import):
            for alias in node.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ImportFrom) and node.module == "os":
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "environ":
                    environ_aliases.add(bound)
                elif alias.name in _OS_ENV_MUTATORS:
                    putenv_aliases.add(bound)
    return os_aliases, environ_aliases, putenv_aliases


def _is_environ_ref(
    node: AST,
    os_aliases: set[str],
    environ_aliases: set[str],
) -> bool:
    if isinstance(node, Name):
        return node.id in environ_aliases
    return (
        isinstance(node, Attribute)
        and node.attr == "environ"
        and isinstance(node.value, Name)
        and node.value.id in os_aliases
    )


def _mutates_environ_subscript(
    target: AST,
    os_aliases: set[str],
    environ_aliases: set[str],
) -> bool:
    return isinstance(target, Subscript) and _is_environ_ref(
        target.value,
        os_aliases,
        environ_aliases,
    )


def check_no_global_environ_mutation(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        os_aliases, environ_aliases, putenv_aliases = _collect_env_aliases(tree)
        if not (os_aliases or environ_aliases or putenv_aliases):
            continue

        for node in walk(tree):
            if isinstance(node, Assign):
                if any(
                    _mutates_environ_subscript(t, os_aliases, environ_aliases)
                    for t in node.targets
                ):
                    findings.append(
                        LintFinding(
                            rule="PL015",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message="Do not mutate global 'os.environ' via item assignment.",
                            hint=(
                                "Avoid mutating process-wide environment variables in "
                                "production code; pass configuration explicitly or use "
                                "the library's Python API inside 'infrastructure'."
                            ),
                        )
                    )
            elif isinstance(node, AugAssign):
                if _mutates_environ_subscript(node.target, os_aliases, environ_aliases):
                    findings.append(
                        LintFinding(
                            rule="PL015",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message="Do not mutate global 'os.environ' via augmented assignment.",
                            hint=(
                                "Avoid mutating process-wide environment variables in "
                                "production code; pass configuration explicitly."
                            ),
                        )
                    )
            elif isinstance(node, Delete):
                if any(
                    _mutates_environ_subscript(t, os_aliases, environ_aliases)
                    for t in node.targets
                ):
                    findings.append(
                        LintFinding(
                            rule="PL015",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message="Do not delete entries from global 'os.environ'.",
                            hint=(
                                "Avoid mutating process-wide environment variables in "
                                "production code."
                            ),
                        )
                    )
            elif isinstance(node, Call):
                if (
                    isinstance(node.func, Attribute)
                    and node.func.attr in _MUTATING_METHODS
                    and _is_environ_ref(node.func.value, os_aliases, environ_aliases)
                ):
                    findings.append(
                        LintFinding(
                            rule="PL015",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                f"Do not mutate global 'os.environ' via "
                                f"'.{node.func.attr}()'."
                            ),
                            hint=(
                                "Avoid mutating process-wide environment variables in "
                                "production code; configure library behavior via explicit "
                                "API calls inside 'infrastructure' instead."
                            ),
                        )
                    )
                elif (
                    isinstance(node.func, Attribute)
                    and node.func.attr in _OS_ENV_MUTATORS
                    and isinstance(node.func.value, Name)
                    and node.func.value.id in os_aliases
                ) or (isinstance(node.func, Name) and node.func.id in putenv_aliases):
                    findings.append(
                        LintFinding(
                            rule="PL015",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message="Do not mutate global process environment via putenv/unsetenv.",
                            hint=(
                                "Avoid mutating process-wide environment variables in "
                                "production code."
                            ),
                        )
                    )
    return findings
