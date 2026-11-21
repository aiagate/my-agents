from ast import Import, ImportFrom, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    parse_file,
    resolve_import_targets,
)

_LAYER_NAMES = (
    "contracts",
    "domain",
    "usecases",
    "infrastructure",
    "presentation",
)


def check_no_adapter_reexport_at_layer_root(
    src_dir: Path = SRC_PACKAGE_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for layer in _LAYER_NAMES:
        init_path = src_dir / layer / "__init__.py"
        if not init_path.exists():
            continue
        tree = parse_file(init_path)
        prefix = f"my_agents.{layer}."
        for node in walk(tree):
            if not isinstance(node, Import | ImportFrom):
                continue
            for target in resolve_import_targets(node, init_path, src_dir):
                if not target.startswith(prefix):
                    continue
                parts = target.split(".")
                is_subpackage = len(parts) >= 4 or (
                    len(parts) == 3 and (src_dir / layer / parts[2]).is_dir()
                )
                if is_subpackage:
                    findings.append(
                        LintFinding(
                            rule="PL017",
                            path=init_path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                f"Layer root '{layer}/__init__.py' must not re-export "
                                f"from subpackage '{target}'."
                            ),
                            hint=(
                                "Keep subpackage/adapter exports in their own "
                                "subpackage '__init__.py' (e.g. 'presentation/cli/__init__.py') "
                                "and avoid exposing adapter details at the layer root."
                            ),
                        )
                    )
    return findings
