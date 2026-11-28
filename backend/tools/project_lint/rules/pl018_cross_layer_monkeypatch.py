from ast import (
    AST,
    Attribute,
    Call,
    Constant,
    Import,
    ImportFrom,
    Name,
    With,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    TESTS_DIR,
    LintFinding,
    dotted_name,
    iter_python_files,
    parse_file,
)


def _subject_module_for_test(
    path: Path,
    tests_dir: Path,
    src_dir: Path,
) -> str:
    rel = path.relative_to(tests_dir)
    mod_name = path.stem.removeprefix("test_")
    candidates: list[tuple[int, str]] = []
    if src_dir.exists():
        for source in iter_python_files(src_dir):
            parts = list(source.relative_to(src_dir).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            joined = "_".join(parts)
            if joined == mod_name or joined.endswith(f"_{mod_name}"):
                candidates.append((len(joined), ".".join(["my_agents", *parts])))
    if candidates:
        return max(candidates)[1]
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


def _is_within_subject(module_path: str, subject_module: str) -> bool:
    return module_path == subject_module or module_path.startswith(f"{subject_module}.")


def _monkeypatch_target_arg(node: Call) -> AST | None:
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == "target":
            return kw.value
    return None


def _monkeypatch_aliases(tree: AST) -> set[str]:
    aliases: set[str] = set()
    for node in walk(tree):
        if not isinstance(node, With):
            continue
        for item in node.items:
            context = item.context_expr
            if (
                isinstance(context, Call)
                and isinstance(context.func, Attribute)
                and context.func.attr == "context"
                and isinstance(context.func.value, Name)
                and context.func.value.id == "monkeypatch"
                and isinstance(item.optional_vars, Name)
            ):
                aliases.add(item.optional_vars.id)
    return aliases


def _patch_imports(tree: AST) -> tuple[set[str], set[str]]:
    patch_names: set[str] = set()
    mock_aliases: set[str] = set()
    for node in walk(tree):
        if isinstance(node, ImportFrom) and node.module == "unittest.mock":
            for alias in node.names:
                if alias.name == "patch":
                    patch_names.add(alias.asname or alias.name)
        elif isinstance(node, ImportFrom) and node.module == "unittest":
            for alias in node.names:
                if alias.name == "mock":
                    mock_aliases.add(alias.asname or alias.name)
        elif isinstance(node, Import) and node.names:
            for alias in node.names:
                if alias.name == "unittest.mock":
                    mock_aliases.add(alias.asname or "mock")
    return patch_names, mock_aliases


def _is_patch_call(
    node: AST,
    patch_names: set[str],
    mock_aliases: set[str],
) -> tuple[bool, bool]:
    if not isinstance(node, Call):
        return False, False
    callee = node.func
    if isinstance(callee, Name):
        return callee.id in patch_names, False
    if not isinstance(callee, Attribute):
        return False, False
    if callee.attr == "patch":
        return isinstance(callee.value, Name) and callee.value.id in mock_aliases, False
    if callee.attr == "object":
        base = dotted_name(callee.value)
        return base in patch_names or (
            isinstance(callee.value, Attribute) and callee.value.attr == "patch"
        ), True
    return False, False


def _private_attribute(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _string_argument(node: Call, index: int) -> str | None:
    if len(node.args) <= index:
        return None
    argument = node.args[index]
    if isinstance(argument, Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def _private_patch_finding(
    path: Path,
    node: Call,
    attribute: str,
) -> LintFinding:
    return LintFinding(
        rule="PL018",
        path=path,
        line=node.lineno,
        col=node.col_offset + 1,
        message=f"Test patches private attribute '{attribute}'.",
        hint=(
            "Never patch private attributes; inject a fake at the public boundary "
            "or assert observable behavior instead of reaching into internals."
        ),
    )


def _cross_module_finding(
    path: Path,
    node: Call,
    target: str,
    subject_module: str,
) -> LintFinding:
    return LintFinding(
        rule="PL018",
        path=path,
        line=node.lineno,
        col=node.col_offset + 1,
        message=(
            f"Test monkeypatches cross-module target '{target}' outside subject "
            f"'{subject_module}'."
        ),
        hint=(
            "Patch only collaborators imported in the test's own subject module "
            f"('{subject_module}.*') or inject fakes at the boundary instead of "
            "coupling to lower-layer internals."
        ),
    )


def _resolved_object_target(
    object_node: AST,
    attribute: str,
    imported: dict[str, str],
) -> str | None:
    dotted = dotted_name(object_node)
    if dotted is None:
        return None
    head, *tail = dotted.split(".")
    resolved_base = imported.get(head)
    if resolved_base is None:
        return None
    return ".".join([resolved_base, *tail, attribute])


def check_no_cross_layer_internal_monkeypatch(
    tests_dir: Path = TESTS_DIR,
    *,
    src_dir: Path | None = None,
) -> list[LintFinding]:
    resolved_src_dir = (
        tests_dir.parent / "src" / "my_agents" if src_dir is None else src_dir
    )
    findings: list[LintFinding] = []
    for path in iter_python_files(tests_dir):
        if not path.name.startswith("test_"):
            continue
        subject_module = _subject_module_for_test(path, tests_dir, resolved_src_dir)
        tree = parse_file(path)
        imported = _collect_my_agents_imports(tree)
        monkeypatch_aliases = _monkeypatch_aliases(tree)
        patch_names, mock_aliases = _patch_imports(tree)

        for node in walk(tree):
            if not isinstance(node, Call):
                continue
            monkeypatch_target = None
            if (
                isinstance(node.func, Attribute)
                and isinstance(node.func.value, Name)
                and (
                    node.func.value.id == "monkeypatch"
                    or node.func.value.id in monkeypatch_aliases
                )
                and node.func.attr in {"setattr", "delattr"}
            ):
                monkeypatch_target = "monkeypatch"
            is_patch, is_patch_object = _is_patch_call(node, patch_names, mock_aliases)
            if monkeypatch_target is None and not is_patch:
                continue

            target_arg = _monkeypatch_target_arg(node)
            if target_arg is None:
                continue

            if is_patch_object:
                attribute = _string_argument(node, 1)
                if attribute is None:
                    continue
                if _private_attribute(attribute):
                    findings.append(_private_patch_finding(path, node, attribute))
                resolved = _resolved_object_target(target_arg, attribute, imported)
                if resolved is not None and not _is_within_subject(
                    resolved.rsplit(".", 1)[0], subject_module
                ):
                    findings.append(
                        _cross_module_finding(path, node, resolved, subject_module)
                    )
                continue

            if isinstance(target_arg, Constant) and isinstance(target_arg.value, str):
                target = target_arg.value
                attribute = target.rsplit(".", 1)[-1]
                if _private_attribute(attribute):
                    findings.append(_private_patch_finding(path, node, attribute))
                if target.startswith("my_agents."):
                    target_module = target.rsplit(".", 1)[0]
                    if not _is_within_subject(target_module, subject_module):
                        findings.append(
                            _cross_module_finding(path, node, target, subject_module)
                        )
                continue

            attribute = _string_argument(node, 1)
            if attribute is None:
                continue
            if _private_attribute(attribute):
                findings.append(_private_patch_finding(path, node, attribute))
            resolved = _resolved_object_target(target_arg, attribute, imported)
            if resolved is not None and not _is_within_subject(
                resolved.rsplit(".", 1)[0], subject_module
            ):
                findings.append(
                    _cross_module_finding(path, node, resolved, subject_module)
                )
    return findings
