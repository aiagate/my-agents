from ast import Attribute, ClassDef, Name, Subscript, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)


def _is_protocol_class(node: ClassDef) -> bool:
    for base in node.bases:
        target = base.value if isinstance(base, Subscript) else base
        if isinstance(target, Name) and target.id == "Protocol":
            return True
        if isinstance(target, Attribute) and target.attr == "Protocol":
            return True
    return False


def _pascal_case(stem: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in stem.split("_") if part)


def check_usecase_protocol_ownership(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    usecases_dir = src_dir / "usecases"
    findings: list[LintFinding] = []
    for path in iter_python_files(usecases_dir):
        if path.name == "__init__.py":
            continue
        expected_name = f"{_pascal_case(path.stem)}UseCase"
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, ClassDef):
                continue
            if node.name.startswith("_") or not node.name.endswith("UseCase"):
                continue
            if not _is_protocol_class(node):
                continue
            if node.name == expected_name:
                continue
            findings.append(
                LintFinding(
                    rule="PL021",
                    path=path,
                    line=node.lineno,
                    col=node.col_offset + 1,
                    message=(
                        f"UseCase protocol '{node.name}' does not match module "
                        f"'{path.stem}' (expected '{expected_name}')."
                    ),
                    hint=(
                        f"Move '{node.name}' to the matching usecase module or prefix "
                        "it with '_' if it is module-local."
                    ),
                )
            )
    return findings
