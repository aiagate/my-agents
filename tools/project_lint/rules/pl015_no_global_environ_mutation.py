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
# reason=Process entrypoint composition root reads ambient env once; expires=2027-11-27; approved-by=human
_ALLOWED_ENVIRON_READ_FILES = frozenset({"shell/server.py"})


def _collect_env_aliases(
    tree: AST,
) -> tuple[set[str], set[str], set[str], set[str]]:
    os_aliases: set[str] = set()
    environ_aliases: set[str] = set()
    putenv_aliases: set[str] = set()
    getenv_aliases: set[str] = set()
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
                elif alias.name == "getenv":
                    getenv_aliases.add(bound)
    return os_aliases, environ_aliases, putenv_aliases, getenv_aliases


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


def _mutation_node_ids(
    tree: AST,
    os_aliases: set[str],
    environ_aliases: set[str],
) -> set[int]:
    mutated: set[int] = set()
    for node in walk(tree):
        if isinstance(node, Assign):
            targets = node.targets
        elif isinstance(node, AugAssign):
            targets = [node.target]
        elif isinstance(node, Delete):
            targets = node.targets
        else:
            continue
        for target in targets:
            if isinstance(target, Subscript):
                for child in walk(target.value):
                    if _is_environ_ref(child, os_aliases, environ_aliases):
                        mutated.add(id(child))
    for node in walk(tree):
        if not isinstance(node, Call) or not isinstance(node.func, Attribute):
            continue
        if node.func.attr not in _MUTATING_METHODS:
            continue
        for child in walk(node.func.value):
            if _is_environ_ref(child, os_aliases, environ_aliases):
                mutated.add(id(child))
    return mutated


def _read_findings(
    tree: AST,
    path: Path,
    os_aliases: set[str],
    environ_aliases: set[str],
    getenv_aliases: set[str],
) -> list[LintFinding]:
    mutated = _mutation_node_ids(tree, os_aliases, environ_aliases)
    findings: list[LintFinding] = []
    for node in walk(tree):
        if isinstance(node, Call):
            callee = node.func
            getenv = (
                isinstance(callee, Attribute)
                and callee.attr == "getenv"
                and isinstance(callee.value, Name)
                and callee.value.id in os_aliases
            ) or (isinstance(callee, Name) and callee.id in getenv_aliases)
            if getenv:
                findings.append(
                    LintFinding(
                        rule="PL015",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message="Do not read global process environment via 'os.getenv()'.",
                        hint=(
                            "Read ambient environment once in 'shell/server.py' and "
                            "inject a Mapping[str, str] into the code under test."
                        ),
                    )
                )
            continue
        if not isinstance(node, Name | Attribute):
            continue
        if id(node) in mutated:
            continue
        if not _is_environ_ref(node, os_aliases, environ_aliases):
            continue
        findings.append(
            LintFinding(
                rule="PL015",
                path=path,
                line=node.lineno,
                col=node.col_offset + 1,
                message="Do not read global 'os.environ' directly.",
                hint=(
                    "Read ambient environment once in 'shell/server.py' and inject "
                    "a Mapping[str, str] into the code under test."
                ),
            )
        )
    return findings


def check_no_global_environ_mutation(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        (
            os_aliases,
            environ_aliases,
            putenv_aliases,
            getenv_aliases,
        ) = _collect_env_aliases(tree)
        if not (os_aliases or environ_aliases or putenv_aliases or getenv_aliases):
            continue
        relative = path.relative_to(src_dir).as_posix()
        if relative not in _ALLOWED_ENVIRON_READ_FILES:
            findings.extend(
                _read_findings(
                    tree,
                    path,
                    os_aliases,
                    environ_aliases,
                    getenv_aliases,
                )
            )

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
                                "the library's Python API inside 'shell'."
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
                                "API calls inside 'shell' instead."
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
