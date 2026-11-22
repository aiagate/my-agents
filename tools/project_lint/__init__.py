from pathlib import Path

from tools.project_lint.common import (
    PROJECT_ROOT,
    SRC_PACKAGE_DIR,
    TESTS_DIR,
    TOOLS_DIR,
    LintFinding,
    LintRule,
)
from tools.project_lint.rules import (
    RULES,
    check_adapter_port_surface,
    check_assert_error_message,
    check_exhaustive_branch,
    check_immutable_frozen_dataclass_fields,
    check_layer_boundaries,
    check_no_adapter_reexport_at_layer_root,
    check_no_cast_in_src,
    check_no_cross_layer_internal_monkeypatch,
    check_no_direct_logging,
    check_no_docstrings,
    check_no_exception_result_errors,
    check_no_global_environ_mutation,
    check_no_inline_suppressions,
    check_no_object_in_contravariant_type_args,
    check_no_plain_str_secrets,
    check_no_private_dataclass_init_fields,
    check_no_private_type_in_public_api,
    check_no_shallow_tests,
    check_strict_coverage_config,
    check_strict_pyright_config,
    check_thin_entrypoint,
    check_tool_descriptions,
    check_usecase_protocol_ownership,
)


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
    "check_adapter_port_surface",
    "check_assert_error_message",
    "check_exhaustive_branch",
    "check_immutable_frozen_dataclass_fields",
    "check_layer_boundaries",
    "check_no_adapter_reexport_at_layer_root",
    "check_no_cast_in_src",
    "check_no_cross_layer_internal_monkeypatch",
    "check_no_direct_logging",
    "check_no_docstrings",
    "check_no_exception_result_errors",
    "check_no_global_environ_mutation",
    "check_no_inline_suppressions",
    "check_no_object_in_contravariant_type_args",
    "check_no_plain_str_secrets",
    "check_no_private_dataclass_init_fields",
    "check_no_private_type_in_public_api",
    "check_no_shallow_tests",
    "check_strict_coverage_config",
    "check_strict_pyright_config",
    "check_thin_entrypoint",
    "check_tool_descriptions",
    "check_usecase_protocol_ownership",
    "main",
    "run_all_checks",
]
