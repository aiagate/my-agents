from ast import AnnAssign, AsyncFunctionDef, ClassDef, FunctionDef, Name, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    annotation_is_non_init_marker,
    base_class_names,
    has_decorator_name,
    iter_python_files,
    parse_file,
)


def _collect_port_contracts(src_dir: Path) -> dict[str, set[str]]:
    ports_dir = src_dir / "contracts" / "ports"
    direct_members: dict[str, set[str]] = {}
    parent_map: dict[str, list[str]] = {}
    for path in iter_python_files(ports_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, ClassDef):
                continue
            bases = base_class_names(node)
            is_port = any(
                base in {"ABC", "Protocol"} for base in bases
            ) or node.name.startswith("I")
            if not is_port:
                continue
            members = {
                stmt.name
                for stmt in node.body
                if isinstance(stmt, FunctionDef | AsyncFunctionDef)
                and not stmt.name.startswith("_")
            }
            direct_members[node.name] = members
            parent_map[node.name] = bases

    resolved: dict[str, set[str]] = {}
    for name in direct_members:
        members: set[str] = set()
        stack = [name]
        visited: set[str] = set()
        while stack:
            current = stack.pop()
            if current in visited or current not in direct_members:
                continue
            visited.add(current)
            members.update(direct_members[current])
            stack.extend(parent_map.get(current, []))
        resolved[name] = members
    return resolved


def check_adapter_port_surface(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    port_contracts = _collect_port_contracts(src_dir)
    if not port_contracts:
        return []
    findings: list[LintFinding] = []
    infra_dir = src_dir / "infrastructure"
    factory_decorators = frozenset({"classmethod", "staticmethod"})
    for path in iter_python_files(infra_dir):
        tree = parse_file(path)
        for node in walk(tree):
            if not isinstance(node, ClassDef):
                continue
            matched_ports = [
                base for base in base_class_names(node) if base in port_contracts
            ]
            if not matched_ports:
                continue
            allowed_members: set[str] = set()
            for port_name in matched_ports:
                allowed_members.update(port_contracts[port_name])
            ports_label = ", ".join(matched_ports)
            for stmt in node.body:
                if isinstance(stmt, FunctionDef | AsyncFunctionDef):
                    if (
                        stmt.name.startswith("_")
                        or has_decorator_name(stmt, factory_decorators)
                        or stmt.name in allowed_members
                    ):
                        continue
                    findings.append(
                        LintFinding(
                            rule="PL013",
                            path=path,
                            line=stmt.lineno,
                            col=stmt.col_offset + 1,
                            message=(
                                f"Adapter '{node.name}' exposes public member "
                                f"'{stmt.name}' not declared on port(s) {ports_label}."
                            ),
                            hint=(
                                "Keep adapter public APIs strictly aligned with their "
                                "port contract (__init__ and @classmethod/@staticmethod "
                                "factories are allowed). Do not add test-only "
                                "properties or methods to adapters; inspect wiring "
                                "via monkeypatch or dependency injection in tests "
                                "instead."
                            ),
                        )
                    )
                elif (
                    isinstance(stmt, AnnAssign)
                    and isinstance(stmt.target, Name)
                    and not stmt.target.id.startswith("_")
                    and not annotation_is_non_init_marker(stmt.annotation)
                    and stmt.target.id not in allowed_members
                ):
                    findings.append(
                        LintFinding(
                            rule="PL013",
                            path=path,
                            line=stmt.lineno,
                            col=stmt.col_offset + 1,
                            message=(
                                f"Adapter '{node.name}' exposes public attribute "
                                f"'{stmt.target.id}' not declared on port(s) "
                                f"{ports_label}."
                            ),
                            hint=(
                                "Keep adapter state private ('_'-prefixed) and align "
                                "public APIs strictly with the port contract."
                            ),
                        )
                    )
    return findings
