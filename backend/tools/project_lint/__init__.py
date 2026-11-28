import sys
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
        sys.stdout.write(finding.format() + "\n")
        sys.stdout.write("\n")
    if findings:
        sys.stdout.write(f"Found {len(findings)} project lint error(s).\n")
        return 1
    sys.stdout.write("All project lint checks passed!\n")
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
