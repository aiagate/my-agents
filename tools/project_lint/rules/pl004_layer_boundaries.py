from ast import Import, ImportFrom, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
    resolve_import_targets,
)

_FORBIDDEN_INTERNAL_LAYERS: dict[str, frozenset[str]] = {
    "contracts": frozenset(
        {"domain", "usecases", "infrastructure", "presentation", "composition"}
    ),
    "domain": frozenset(
        {"contracts", "usecases", "infrastructure", "presentation", "composition"}
    ),
    "usecases": frozenset({"infrastructure", "presentation", "composition"}),
    "infrastructure": frozenset({"usecases", "presentation", "composition"}),
    "presentation": frozenset({"infrastructure", "composition"}),
}

_FORBIDDEN_EXTERNAL_LIBS: dict[str, frozenset[str]] = {
    "contracts": frozenset({"pydantic_ai", "dotenv"}),
    "domain": frozenset({"pydantic_ai", "dotenv"}),
    "usecases": frozenset({"pydantic_ai", "dotenv"}),
    "presentation": frozenset({"pydantic_ai", "dotenv"}),
}


def _layer_of(path: Path, src_dir: Path) -> str | None:
    rel = path.relative_to(src_dir)
    if len(rel.parts) < 2:
        return None
    return rel.parts[0]


def check_layer_boundaries(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        layer = _layer_of(path, src_dir)
        if layer is None:
            continue
        forbidden_internal = _FORBIDDEN_INTERNAL_LAYERS.get(layer, frozenset())
        forbidden_external = _FORBIDDEN_EXTERNAL_LIBS.get(layer, frozenset())
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, Import | ImportFrom):
                continue
            for target in resolve_import_targets(node, path, src_dir):
                parts = target.split(".")
                if parts[0] in forbidden_external:
                    findings.append(
                        LintFinding(
                            rule="PL004",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                f"Layer '{layer}' must not import external library "
                                f"'{parts[0]}'."
                            ),
                            hint=(
                                "Keep framework/SDK dependencies inside "
                                "'infrastructure' behind a port in 'contracts/ports'."
                            ),
                        )
                    )
                elif (
                    len(parts) >= 2
                    and parts[0] == "my_agents"
                    and parts[1] in forbidden_internal
                ):
                    findings.append(
                        LintFinding(
                            rule="PL004",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                f"Layer '{layer}' must not import from '{parts[1]}'."
                            ),
                            hint=(
                                "Invert the dependency via a port in 'contracts/ports' "
                                "and wire concrete implementations in 'composition.py'."
                            ),
                        )
                    )
    return findings
