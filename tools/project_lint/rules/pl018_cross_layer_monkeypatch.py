from ast import AST, Attribute, Call, Constant, Import, ImportFrom, Name, walk
from pathlib import Path

from tools.project_lint.common import (
    TESTS_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
)


def _subject_module_for_test(path: Path, tests_dir: Path) -> str:
    rel = path.relative_to(tests_dir)
    mod_name = path.stem.removeprefix("test_")
    return ".".join(["my_agents", *rel.parent.parts, mod_name])


def _collect_my_agents_imports(tree: AST) -> dict[str, str]:
    imported: dict[str, str] = {}
    for node in walk(tree):
        if isinstance(node, Import):
            for alias in node.names:
                if alias.name == "my_agents" or alias.name.startswith("my_agents."):
                    bound = alias.asname or alias.name.split(".", 1)[0]
                    imported[bound] = (
                        alias.name if alias.asname else alias.name.split(".", 1)[0]
                    )
        elif (
            isinstance(node, ImportFrom)
            and node.level == 0
            and node.module is not None
            and (node.module == "my_agents" or node.module.startswith("my_agents."))
        ):
            for alias in node.names:
                bound = alias.asname or alias.name
                imported[bound] = f"{node.module}.{alias.name}"
    return imported


def _dotted_name(node: AST) -> str | None:
    if isinstance(node, Name):
        return node.id
    if isinstance(node, Attribute):
        parent = _dotted_name(node.value)
        if parent is not None:
            return f"{parent}.{node.attr}"
    return None


def _is_within_subject(module_path: str, subject_module: str) -> bool:
    return module_path == subject_module or module_path.startswith(f"{subject_module}.")


def _monkeypatch_target_arg(node: Call) -> AST | None:
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == "target":
            return kw.value
    return None


def check_no_cross_layer_internal_monkeypatch(
    tests_dir: Path = TESTS_DIR,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(tests_dir):
        if not path.name.startswith("test_"):
            continue
        subject_module = _subject_module_for_test(path, tests_dir)
        tree = parse_file(path)
        imported = _collect_my_agents_imports(tree)

        for node in walk(tree):
            if not (
                isinstance(node, Call)
                and isinstance(node.func, Attribute)
                and isinstance(node.func.value, Name)
                and node.func.value.id == "monkeypatch"
                and node.func.attr in {"setattr", "delattr"}
            ):
                continue

            target_arg = _monkeypatch_target_arg(node)
            if target_arg is None:
                continue

            if (
                isinstance(target_arg, Constant)
                and isinstance(target_arg.value, str)
                and target_arg.value.startswith("my_agents.")
            ):
                target_module = target_arg.value.rsplit(".", 1)[0]
                if not _is_within_subject(target_module, subject_module):
                    findings.append(
                        LintFinding(
                            rule="PL018",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                f"Test monkeypatches cross-module target "
                                f"'{target_arg.value}' outside subject '{subject_module}'."
                            ),
                            hint=(
                                "Patch only collaborators imported in the test's own "
                                f"subject module ('{subject_module}.*') or inject fakes "
                                "at the boundary instead of coupling to lower-layer "
                                "internals."
                            ),
                        )
                    )
            else:
                dotted = _dotted_name(target_arg)
                if dotted is None:
                    continue
                head, *tail = dotted.split(".")
                resolved_base = imported.get(head)
                if resolved_base is None:
                    continue
                resolved_target = ".".join([resolved_base, *tail])
                if not _is_within_subject(resolved_target, subject_module):
                    findings.append(
                        LintFinding(
                            rule="PL018",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                f"Test monkeypatches cross-module object "
                                f"'{resolved_target}' outside subject '{subject_module}'."
                            ),
                            hint=(
                                "Patch only collaborators on the test's own subject "
                                f"module ('{subject_module}.*') or inject fakes at the "
                                "boundary."
                            ),
                        )
                    )
    return findings
