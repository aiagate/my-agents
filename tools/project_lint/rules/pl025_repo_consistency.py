import builtins
import json
import os
import re
import sqlite3
import subprocess
import tomllib
from ast import (
    Assign,
    AsyncFunctionDef,
    Constant,
    FunctionDef,
    ImportFrom,
    List,
    Name,
    Tuple,
    walk,
)
from pathlib import Path

from tools.project_lint.common import (
    PROJECT_ROOT,
    LintFinding,
    as_table,
    iter_python_files,
    parse_file,
    resolve_import_targets,
)

_CODE = re.compile(r"PL(\d{3})")
_RANGE = re.compile(r"PL(\d{3})\s*[〜~\-]\s*PL(\d{3})")
_ISSUE_COMMENT = re.compile(
    r"\d{4}-\d{2}-\d{2}\s*\|.+\|\s*(?:base\s+)?SHA\s+[0-9a-f]{7,40}"
)
_ERROR_NAME = re.compile(r"\b([A-Z][A-Za-z0-9_]*Error)\b")
_MY_AGENTS_PATH = re.compile(r"\bmy_agents\.([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)")
_PATH_PREFIXES = (
    "src/",
    "tests/",
    "tools/",
    ".githooks/",
    ".github/",
    ".agents/",
)
_PLACEHOLDERS = ("<", ">", "*", "...", "…")
_TYPO = re.compile(r"(はの|がの|をの|にの)")


def _finding(
    path: Path,
    line: int,
    message: str,
    hint: str,
) -> LintFinding:
    return LintFinding(
        rule="PL025",
        path=path,
        line=line,
        col=1,
        message=message,
        hint=hint,
    )


def _registered_codes() -> set[str]:
    from tools.project_lint.rules import RULES

    return {rule.code for rule in RULES}


def _rule_files(root: Path) -> list[Path]:
    rules_dir = root / "tools" / "project_lint" / "rules"
    return [path for path in iter_python_files(rules_dir) if path.name.startswith("pl")]


def _issue_comment(lines: list[str], definition_line: int) -> str:
    collected: list[str] = []
    index = definition_line - 1
    while index >= 0 and len(collected) < 3:
        stripped = lines[index].strip()
        if stripped.startswith("#"):
            collected.append(stripped)
            index -= 1
            continue
        if not stripped and not collected:
            index -= 1
            continue
        break
    if collected:
        return "\n".join(reversed(collected))
    for offset in range(1, 4):
        position = definition_line + offset
        if position < len(lines) and lines[position].strip().startswith("#"):
            collected.append(lines[position].strip())
    return "\n".join(collected)


def _regression_findings(root: Path, codes: set[str]) -> list[LintFinding]:
    tests_file = root / "tests" / "test_project_lint_rules.py"
    if not tests_file.exists():
        return []
    content = tests_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    findings: list[LintFinding] = []
    for code in sorted(codes):
        pattern = re.compile(rf"^def test_{code.lower()}_regression_\w+", re.MULTILINE)
        match = pattern.search(content)
        if match is None:
            findings.append(
                _finding(
                    tests_file,
                    1,
                    f"Missing regression test for '{code}'.",
                    f"Add 'def test_{code.lower()}_regression_<class>' with an issue comment.",
                )
            )
            continue
        definition_line = content[: match.start()].count("\n")
        comment = _issue_comment(lines, definition_line)
        if _ISSUE_COMMENT.search(comment) is None:
            findings.append(
                _finding(
                    tests_file,
                    definition_line + 1,
                    f"Regression test for '{code}' lacks provenance metadata.",
                    (
                        "Add a '#' comment directly above the test with "
                        "YYYY-MM-DD | origin/thread | SHA."
                    ),
                )
            )
    return findings


def _source_class_names(root: Path) -> set[str]:
    names: set[str] = set()
    for path in iter_python_files(root / "src" / "my_agents"):
        for node in walk(parse_file(path)):
            if isinstance(node, (FunctionDef, AsyncFunctionDef)):
                continue
            name = getattr(node, "name", None)
            if isinstance(name, str):
                names.add(name)
    return names


def _type_exists(name: str, source_names: set[str]) -> bool:
    if name in source_names:
        return True
    for module in (builtins, json, sqlite3):
        if hasattr(module, name):
            return True
    try:
        import flow_res
        import pydantic
    except ImportError:
        return False
    return hasattr(flow_res, name) or hasattr(pydantic, name)


def _old_reference_findings(root: Path) -> list[LintFinding]:
    source_names = _source_class_names(root)
    findings: list[LintFinding] = []
    for path in _rule_files(root):
        for node in walk(parse_file(path)):
            if not isinstance(node, Constant) or not isinstance(node.value, str):
                continue
            for match in _ERROR_NAME.finditer(node.value):
                name = match.group(1)
                if _type_exists(name, source_names):
                    continue
                findings.append(
                    _finding(
                        path,
                        node.lineno,
                        f"String literal references missing error type '{name}'.",
                        "Use an existing AppError-based type or remove the stale name.",
                    )
                )
            for match in _MY_AGENTS_PATH.finditer(node.value):
                relative = match.group(1).replace(".", "/")
                module = root / "src" / "my_agents" / relative
                if module.with_suffix(".py").exists() or module.is_dir():
                    continue
                findings.append(
                    _finding(
                        path,
                        node.lineno,
                        f"String literal references missing module 'my_agents.{match.group(1)}'.",
                        "Point the string at an existing 'src/my_agents' module.",
                    )
                )
    return findings


def _agents_table_codes(root: Path) -> set[str]:
    agents = root / "AGENTS.md"
    if not agents.exists():
        return set()
    codes: set[str] = set()
    for line in agents.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        if "廃止" in line or "欠番" in line:
            continue
        codes.update(f"PL{code}" for code in _CODE.findall(line))
    return codes


def _markdown_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.md"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        files = [root / line for line in result.stdout.splitlines() if line]
        return [path for path in files if path.exists()]
    return sorted(
        path
        for path in root.rglob("*.md")
        if ".git" not in path.parts and ".venv" not in path.parts
    )


def _range_findings(root: Path, codes: set[str]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in _markdown_files(root):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in _RANGE.finditer(line):
                low, high = sorted((int(match.group(1)), int(match.group(2))))
                if all(f"PL{value:03d}" in codes for value in range(low, high + 1)):
                    continue
                findings.append(
                    _finding(
                        path,
                        line_number,
                        f"Range '{match.group(0)}' crosses retired rule numbers.",
                        "List active rules explicitly instead of spanning gaps.",
                    )
                )
    return findings


def _path_findings(root: Path) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in _markdown_files(root):
        content = path.read_text(encoding="utf-8")
        for match in re.finditer(r"`([^`\n]+)`", content):
            candidate = match.group(1)
            if not candidate.startswith(_PATH_PREFIXES):
                continue
            if any(marker in candidate for marker in _PLACEHOLDERS):
                continue
            if (root / candidate).exists():
                continue
            line_number = content[: match.start()].count("\n") + 1
            findings.append(
                _finding(
                    path,
                    line_number,
                    f"Documented path '{candidate}' does not exist.",
                    "Update the document to an existing tracked path.",
                )
            )
    return findings


def _hook_findings(root: Path) -> list[LintFinding]:
    hooks_dir = root / ".githooks"
    agents = root / "AGENTS.md"
    workflow = root / ".github" / "workflows" / "validate.yml"
    if not hooks_dir.exists() or not agents.exists() or not workflow.exists():
        return []
    agents_text = agents.read_text(encoding="utf-8")
    workflow_text = workflow.read_text(encoding="utf-8")
    findings: list[LintFinding] = []
    for hook in sorted(hooks_dir.iterdir()):
        if not hook.is_file():
            continue
        if not os.access(hook, os.X_OK) and hook.name not in {
            "commit-msg",
            "pre-commit",
        }:
            continue
        if hook.name not in agents_text:
            findings.append(
                _finding(
                    agents,
                    1,
                    f"Hook '{hook.name}' is not documented in AGENTS.md.",
                    "Add the hook to the enforcement table.",
                )
            )
        if hook.name not in workflow_text:
            findings.append(
                _finding(
                    workflow,
                    1,
                    f"Hook '{hook.name}' is not executed by the CI workflow.",
                    "Run the hook from '.github/workflows/validate.yml'.",
                )
            )
    return findings


def _typo_findings(root: Path) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in _markdown_files(root):
        in_fence = False
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            stripped = re.sub(r"`[^`]*`", "", line)
            if _TYPO.search(stripped) is None:
                continue
            findings.append(
                _finding(
                    path,
                    line_number,
                    "Duplicated Japanese particle typo found.",
                    "Remove the stray particle (はの/がの/をの/にの).",
                )
            )
    return findings


def _all_export_findings(root: Path) -> list[LintFinding]:
    src_dir = root / "src" / "my_agents"
    exported: dict[str, set[str]] = {}
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        names: set[str] = set()
        for node in walk(tree):
            if not isinstance(node, Assign):
                continue
            if not any(
                isinstance(target, Name) and target.id == "__all__"
                for target in node.targets
            ):
                continue
            if isinstance(node.value, List | Tuple):
                names.update(
                    item.value
                    for item in node.value.elts
                    if isinstance(item, Constant) and isinstance(item.value, str)
                )
        if not names:
            continue
        parts = list(path.relative_to(src_dir).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        exported[".".join(["my_agents", *parts])] = names
    findings: list[LintFinding] = []
    directories = [src_dir, root / "tests"]
    for directory in directories:
        for path in iter_python_files(directory):
            for node in walk(parse_file(path)):
                if not isinstance(node, ImportFrom):
                    continue
                targets = resolve_import_targets(node, path, src_dir)
                if not targets or targets[0] not in exported:
                    continue
                allowed = exported[targets[0]]
                for alias in node.names:
                    if alias.name == "*" or alias.name in allowed:
                        continue
                    if alias.name.startswith("__") and alias.name.endswith("__"):
                        continue
                    findings.append(
                        _finding(
                            path,
                            node.lineno,
                            (
                                f"'{targets[0]}' imports '{alias.name}' which is "
                                "not in __all__."
                            ),
                            "Export the symbol from the source module or stop importing it.",
                        )
                    )
    return findings


def _readme_tree_findings(root: Path) -> list[LintFinding]:
    readme = root / "README.md"
    if not readme.exists():
        return []
    content = readme.read_text(encoding="utf-8")
    blocks = re.findall(r"```[^\n]*\n(.*?)```", content, re.DOTALL)
    tree = next((block for block in blocks if "src/my_agents/" in block), None)
    if tree is None:
        return [
            _finding(
                readme,
                1,
                "README.md has no repository tree block.",
                "Add a fenced tree that lists the package and tools modules.",
            )
        ]
    findings: list[LintFinding] = []
    src_dir = root / "src" / "my_agents"
    for layer in ("core", "shell"):
        for path in iter_python_files(src_dir / layer):
            if (
                path.name == "__init__.py"
                and not path.read_text(encoding="utf-8").strip()
            ):
                continue
            if path.name not in tree:
                findings.append(
                    _finding(
                        readme,
                        1,
                        f"README tree is missing 'src/my_agents/{layer}/{path.name}'.",
                        "List every production module in the README tree.",
                    )
                )
    tools_dir = root / "tools"
    if tools_dir.exists():
        for path in sorted(tools_dir.iterdir()):
            if path.is_dir() and path.name == "__pycache__":
                continue
            if (path.is_dir() or path.suffix == ".py") and path.name not in tree:
                findings.append(
                    _finding(
                        readme,
                        1,
                        f"README tree is missing 'tools/{path.name}'.",
                        "List tools modules and subpackages in the README tree.",
                    )
                )
    return findings


def _normalize_package(name: str) -> str:
    base = name.split("[", 1)[0]
    base = re.split(r"[<>=!~]", base, maxsplit=1)[0]
    return base.strip().lower().replace("_", "-")


def _library_findings(root: Path) -> list[LintFinding]:
    agents = root / "AGENTS.md"
    pyproject = root / "pyproject.toml"
    if not agents.exists() or not pyproject.exists():
        return []
    dependencies: set[str] = set()
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = as_table(data.get("project"))
    if project is not None:
        declared = project.get("dependencies")
        if isinstance(declared, list):
            for dependency in declared:
                if isinstance(dependency, str):
                    dependencies.add(_normalize_package(dependency))
    lines = agents.read_text(encoding="utf-8").splitlines()
    in_section = False
    findings: list[LintFinding] = []
    for line_number, line in enumerate(lines, start=1):
        if line.strip() == "# 主要ライブラリ":
            in_section = True
            continue
        if in_section and line.startswith("#"):
            break
        if not in_section or not line.lstrip().startswith("|"):
            continue
        match = re.match(r"\|\s*`([^`]+)`\s*\|", line)
        if match is None:
            continue
        name = _normalize_package(match.group(1))
        if name in dependencies:
            continue
        findings.append(
            _finding(
                agents,
                line_number,
                f"Library '{match.group(1)}' is not a project dependency.",
                "List only libraries declared in '[project].dependencies'.",
            )
        )
    return findings


def _table_findings(root: Path, codes: set[str]) -> list[LintFinding]:
    agents = root / "AGENTS.md"
    table_codes = _agents_table_codes(root)
    findings: list[LintFinding] = []
    if table_codes == codes:
        return findings
    for code in sorted(codes - table_codes):
        findings.append(
            _finding(
                agents,
                1,
                f"Rule '{code}' is registered but missing from AGENTS.md.",
                "Add the registered rule to the AGENTS.md enforcement table.",
            )
        )
    for code in sorted(table_codes - codes):
        findings.append(
            _finding(
                agents,
                1,
                f"AGENTS.md lists unregistered rule '{code}'.",
                "Register the rule or remove the stale table row.",
            )
        )
    return findings


def check_regression_provenance(
    root: Path = PROJECT_ROOT,
    *,
    codes: set[str] | None = None,
) -> list[LintFinding]:
    return _regression_findings(root, _registered_codes() if codes is None else codes)


def check_stale_references(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    return _old_reference_findings(root)


def check_table_sync(
    root: Path = PROJECT_ROOT,
    *,
    codes: set[str] | None = None,
) -> list[LintFinding]:
    return _table_findings(root, _registered_codes() if codes is None else codes)


def check_range_notation(
    root: Path = PROJECT_ROOT,
    *,
    codes: set[str] | None = None,
) -> list[LintFinding]:
    return _range_findings(root, _registered_codes() if codes is None else codes)


def check_document_paths(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    return _path_findings(root)


def check_hook_sync(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    return _hook_findings(root)


def check_particle_typos(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    return _typo_findings(root)


def check_all_exports(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    return _all_export_findings(root)


def check_readme_tree(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    return _readme_tree_findings(root)


def check_library_table(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    return _library_findings(root)


def check_repo_consistency(
    root: Path = PROJECT_ROOT,
) -> list[LintFinding]:
    codes = _registered_codes()
    findings: list[LintFinding] = []
    findings.extend(_regression_findings(root, codes))
    findings.extend(_old_reference_findings(root))
    findings.extend(_table_findings(root, codes))
    findings.extend(_range_findings(root, codes))
    findings.extend(_path_findings(root))
    findings.extend(_hook_findings(root))
    findings.extend(_typo_findings(root))
    findings.extend(_all_export_findings(root))
    findings.extend(_readme_tree_findings(root))
    findings.extend(_library_findings(root))
    return findings
