import re
import tomllib
from ast import (
    AnnAssign,
    Assign,
    Call,
    Dict,
    List,
    Module,
    Name,
    Set,
    Tuple,
)
from datetime import UTC, date, datetime
from pathlib import Path

from tools.project_lint.common import (
    PROJECT_ROOT,
    LintFinding,
    as_table,
    iter_python_files,
    parse_file,
)

_METADATA = re.compile(
    r"#\s*reason=(?P<reason>.+?);\s*expires=(?P<expires>\d{4}-\d{2}-\d{2});"
    r"\s*approved-by=(?P<approver>\S+)"
)
_GOVERNED_CONSTANT = re.compile(r"^(_ALLOWED_|_NON_SECRET_|_RUNTIME_)")


def _today() -> date:
    return datetime.now(UTC).date()


def _metadata_for_line(lines: list[str], line_number: int) -> re.Match[str] | None:
    for offset in range(1, 3):
        index = line_number - 1 - offset
        if index < 0:
            break
        match = _METADATA.search(lines[index])
        if match is not None:
            return match
    return None


def _metadata_finding(
    path: Path,
    line: int,
    label: str,
    match: re.Match[str] | None,
) -> LintFinding | None:
    if match is None:
        return LintFinding(
            rule="PL024",
            path=path,
            line=line,
            col=1,
            message=(f"Exception '{label}' has no inline governance metadata comment."),
            hint=(
                "Add '# reason=...; expires=YYYY-MM-DD; approved-by=...' directly "
                "above the exception entry."
            ),
        )
    try:
        expires = date.fromisoformat(match.group("expires"))
    except ValueError:
        expires = date.min
    if expires < _today():
        return LintFinding(
            rule="PL024",
            path=path,
            line=line,
            col=1,
            message=f"Exception '{label}' expired on {expires.isoformat()}.",
            hint=(
                "Renew the exception with human approval or remove it and fix the "
                "underlying violation."
            ),
        )
    return None


def _section_bounds(content: str, header: str) -> tuple[int, int] | None:
    lines = content.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == header:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip().startswith("["):
            end = index
            break
    return start, end


def _entry_line(
    content: str,
    header: str,
    entry: str,
    used: set[int],
) -> int | None:
    bounds = _section_bounds(content, header)
    if bounds is None:
        return None
    start, end = bounds
    lines = content.splitlines()
    escaped = entry.replace("\\", "\\\\")
    for index in range(start, end):
        if index in used:
            continue
        line = lines[index]
        if f'"{entry}"' in line or f'"{escaped}"' in line:
            used.add(index)
            return index + 1
    return None


def _check_entries(
    path: Path,
    content: str,
    header: str,
    entries: object,
    used: set[int],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    if not isinstance(entries, list):
        return findings
    lines = content.splitlines()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        line_number = _entry_line(content, header, entry, used)
        if line_number is None:
            continue
        finding = _metadata_finding(
            path,
            line_number,
            entry,
            _metadata_for_line(lines, line_number),
        )
        if finding is not None:
            findings.append(finding)
    return findings


def _non_empty_value(value: object) -> bool:
    if isinstance(value, Call):
        return bool(value.args) or bool(value.keywords)
    if isinstance(value, List | Tuple | Set):
        return bool(value.elts)
    if isinstance(value, Dict):
        return bool(value.keys)
    return True


def _governed_constants(path: Path) -> list[tuple[str, int]]:
    tree = parse_file(path)
    if not isinstance(tree, Module):
        return []
    constants: list[tuple[str, int]] = []
    for stmt in tree.body:
        if isinstance(stmt, Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], Name):
                continue
            name = stmt.targets[0].id
            value = stmt.value
        elif isinstance(stmt, AnnAssign) and isinstance(stmt.target, Name):
            name = stmt.target.id
            value = stmt.value
        else:
            continue
        if value is None or _GOVERNED_CONSTANT.match(name) is None:
            continue
        if _non_empty_value(value):
            constants.append((name, stmt.lineno))
    return constants


def _constant_findings(root: Path) -> list[LintFinding]:
    findings: list[LintFinding] = []
    rules_dir = root / "tools" / "project_lint" / "rules"
    for path in iter_python_files(rules_dir):
        if not path.name.startswith("pl"):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for name, line_number in _governed_constants(path):
            finding = _metadata_finding(
                path,
                line_number,
                name,
                _metadata_for_line(lines, line_number),
            )
            if finding is not None:
                findings.append(finding)
    return findings


def check_exception_governance(
    root: Path = PROJECT_ROOT,
) -> list[LintFinding]:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []
    findings: list[LintFinding] = []
    content = pyproject.read_text(encoding="utf-8")
    lines = content.splitlines()
    root_table = as_table(tomllib.loads(content))
    tool = as_table(root_table.get("tool")) if root_table is not None else None
    ruff = as_table(tool.get("ruff")) if tool is not None else None
    lint = as_table(ruff.get("lint")) if ruff is not None else None

    if lint is not None and "per-file-ignores" in lint:
        line_number = 1
        for index, line in enumerate(lines):
            if line.strip() == "[tool.ruff.lint.per-file-ignores]":
                line_number = index + 1
                break
        findings.append(
            LintFinding(
                rule="PL024",
                path=pyproject,
                line=line_number,
                col=1,
                message="[tool.ruff.lint.per-file-ignores] is forbidden.",
                hint=(
                    "Fix the underlying violation instead of suppressing rules "
                    "per file; inline suppressions are also banned."
                ),
            )
        )

    used: set[int] = set()
    if lint is not None:
        findings.extend(
            _check_entries(
                pyproject,
                content,
                "[tool.ruff.lint]",
                lint.get("ignore"),
                used,
            )
        )

    coverage = as_table(tool.get("coverage")) if tool is not None else None
    run_table = as_table(coverage.get("run")) if coverage is not None else None
    report_table = as_table(coverage.get("report")) if coverage is not None else None
    if run_table is not None:
        findings.extend(
            _check_entries(
                pyproject,
                content,
                "[tool.coverage.run]",
                run_table.get("omit"),
                used,
            )
        )
    if report_table is not None:
        findings.extend(
            _check_entries(
                pyproject,
                content,
                "[tool.coverage.report]",
                report_table.get("exclude_also"),
                used,
            )
        )
    findings.extend(_constant_findings(root))
    return findings
