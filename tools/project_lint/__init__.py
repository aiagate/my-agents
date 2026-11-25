from pathlib import Path

from tools.project_lint.common import (
    PROJECT_ROOT,
    SRC_PACKAGE_DIR,
    TESTS_DIR,
    TOOLS_DIR,
    LintFinding,
    LintRule,
)
from tools.project_lint.rules import RULES


def run_all_checks(root: Path = PROJECT_ROOT) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for rule in RULES:
        findings.extend(rule.check_root(root))
    return findings


def main() -> int:
    findings = run_all_checks()
    for finding in findings:
        print(finding.format())
        print()
    if findings:
        print(f"Found {len(findings)} project lint error(s).")
        return 1
    print("All project lint checks passed!")
    return 0


__all__ = [
    "PROJECT_ROOT",
    "RULES",
    "SRC_PACKAGE_DIR",
    "TESTS_DIR",
    "TOOLS_DIR",
    "LintFinding",
    "LintRule",
    "main",
    "run_all_checks",
]
