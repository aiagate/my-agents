from ast import Import, ImportFrom, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
    resolve_import_targets,
)

_CORE_FORBIDDEN_EXTERNAL = frozenset(
    {
        "asyncio",
        "dotenv",
        "fastmcp",
        "http",
        "mcp",
        "os",
        "pydantic_ai",
        "pydantic_settings",
        "socket",
        "sqlite3",
        "starlette",
        "subprocess",
        "uvicorn",
    }
)


def _layer_of(path: Path, src_dir: Path) -> str | None:
    rel = path.relative_to(src_dir)
    return None if len(rel.parts) < 2 else rel.parts[0]


def _finding(
    path: Path, node: Import | ImportFrom, message: str, hint: str
) -> LintFinding:
    return LintFinding(
        rule="PL004",
        path=path,
        line=node.lineno,
        col=node.col_offset + 1,
        message=message,
        hint=hint,
    )


def check_layer_boundaries(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        layer = _layer_of(path, src_dir)
        for node in walk(parse_file(path)):
            if not isinstance(node, Import | ImportFrom):
                continue
            for target in resolve_import_targets(node, path, src_dir):
                parts = target.split(".")
                if layer == "core":
                    if parts[0] in _CORE_FORBIDDEN_EXTERNAL:
                        findings.append(
                            _finding(
                                path,
                                node,
                                f"'core' must not import external I/O library '{parts[0]}'.",
                                "Move the effect into 'shell' and pass a value into core.",
                            )
                        )
                    elif len(parts) >= 2 and parts[:2] == ["my_agents", "shell"]:
                        findings.append(
                            _finding(
                                path,
                                node,
                                "'core' must not import from 'shell'.",
                                "Invert the dependency: shell imports core, never the reverse.",
                            )
                        )
                elif (
                    layer is None
                    and path.name == "__init__.py"
                    and not (len(parts) >= 2 and parts[:2] == ["my_agents", "shell"])
                ):
                    findings.append(
                        _finding(
                            path,
                            node,
                            f"Entrypoint '__init__.py' must not import '{target}'.",
                            "Keep the entrypoint limited to a thin delegation into 'my_agents.shell'.",
                        )
                    )
    return findings
