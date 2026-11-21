from ast import Attribute, Name, Subscript, Tuple, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)

_CONTRAVARIANT_TYPE_ARG_POSITIONS: dict[str, tuple[int, ...]] = {
    "Agent": (0,),
}


def check_no_object_in_contravariant_type_args(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, Subscript):
                continue
            type_name = (
                node.value.id
                if isinstance(node.value, Name)
                else node.value.attr
                if isinstance(node.value, Attribute)
                else ""
            )
            positions = _CONTRAVARIANT_TYPE_ARG_POSITIONS.get(type_name)
            if positions is None:
                continue
            args = (
                list(node.slice.elts) if isinstance(node.slice, Tuple) else [node.slice]
            )
            for pos in positions:
                if pos >= len(args):
                    continue
                target = args[pos]
                if isinstance(target, Name) and target.id == "object":
                    findings.append(
                        LintFinding(
                            rule="PL012",
                            path=path,
                            line=target.lineno,
                            col=target.col_offset + 1,
                            message=(
                                f"Generic type '{type_name}' uses 'object' in "
                                f"contravariant type parameter position {pos}."
                            ),
                            hint=(
                                "Contravariant parameters invert subtyping (e.g. "
                                "Agent[object, str] rejects Agent[None, str]). "
                                "Use 'None' when no deps are passed, or a concrete "
                                "deps type instead of 'object'."
                            ),
                        )
                    )
    return findings
