from ast import Attribute, Call, Import, ImportFrom, Name, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)


def check_no_cast_in_src(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        typing_aliases: set[str] = set()
        for node in walk(tree):
            if isinstance(node, Import):
                for alias in node.names:
                    if alias.name == "typing":
                        typing_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ImportFrom) and node.module == "typing":
                for alias in node.names:
                    if alias.name == "cast":
                        findings.append(
                            LintFinding(
                                rule="PL009",
                                path=path,
                                line=node.lineno,
                                col=node.col_offset + 1,
                                message=(
                                    "Do not import 'typing.cast' in production code."
                                ),
                                hint=(
                                    "Narrow types with isinstance, TypeIs, or fix "
                                    "the underlying type design instead of casting."
                                ),
                            )
                        )
        for node in walk(tree):
            if (
                isinstance(node, Call)
                and isinstance(node.func, Attribute)
                and node.func.attr == "cast"
                and isinstance(node.func.value, Name)
                and node.func.value.id in typing_aliases
            ):
                findings.append(
                    LintFinding(
                        rule="PL009",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message="Do not call 'typing.cast' in production code.",
                        hint=(
                            "Narrow types with isinstance, TypeIs, or fix the "
                            "underlying type design instead of casting."
                        ),
                    )
                )
    return findings
