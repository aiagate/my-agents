import builtins
from ast import AST, Attribute, ClassDef, Name, Subscript, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)

_BUILTIN_EXCEPTION_NAMES = frozenset(
    name
    for name, obj in vars(builtins).items()
    if isinstance(obj, type) and issubclass(obj, BaseException)
)


def _base_name(base: AST) -> str | None:
    target = base.value if isinstance(base, Subscript) else base
    if isinstance(target, Name):
        return target.id
    if isinstance(target, Attribute):
        return target.attr
    return None


def _is_exception_base(base_name: str, known_exceptions: set[str]) -> bool:
    return (
        base_name in known_exceptions
        or base_name in _BUILTIN_EXCEPTION_NAMES
        or base_name.endswith("Exception")
    )


def check_no_exception_result_errors(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        classes = [node for node in walk(tree) if isinstance(node, ClassDef)]
        flagged_names: set[str] = set()

        changed = True
        while changed:
            changed = False
            for node in classes:
                if node.name in flagged_names:
                    continue
                for base in node.bases:
                    name = _base_name(base)
                    if name is not None and _is_exception_base(name, flagged_names):
                        flagged_names.add(node.name)
                        findings.append(
                            LintFinding(
                                rule="PL019",
                                path=path,
                                line=node.lineno,
                                col=node.col_offset + 1,
                                message=(
                                    f"Class '{node.name}' subclasses exception "
                                    f"'{name}' in production code."
                                ),
                                hint=(
                                    "Model errors as frozen dataclass value objects "
                                    "implementing AppError/TypedError/DisplayableError "
                                    "and return them via flow_res.Result instead of "
                                    "defining Exception subclasses."
                                ),
                            )
                        )
                        changed = True
                        break
    return findings
