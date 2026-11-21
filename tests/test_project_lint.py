import pytest

from tools.project_lint import PROJECT_ROOT, RULES, LintFinding, LintRule


def _assert_no_findings(findings: list[LintFinding]) -> None:
    if findings:
        formatted = "\n\n".join(finding.format() for finding in findings)
        raise AssertionError(f"Project lint violations found:\n\n{formatted}")


@pytest.mark.parametrize(
    "rule",
    RULES,
    ids=[f"{rule.code}:{rule.name}" for rule in RULES],
)
def test_project_lint_rule(rule: LintRule) -> None:
    _assert_no_findings(rule.check_root(PROJECT_ROOT))
