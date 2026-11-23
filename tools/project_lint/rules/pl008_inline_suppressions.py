import io
import re
import tokenize
from collections.abc import Sequence
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    TESTS_DIR,
    TOOLS_DIR,
    LintFinding,
    iter_python_files,
)

_INLINE_SUPPRESSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("type: ignore", re.compile(r"\btype:\s*ignore\b", re.IGNORECASE)),
    ("ty: ignore", re.compile(r"\bty:\s*ignore\b", re.IGNORECASE)),
    ("noqa", re.compile(r"\bnoqa\b", re.IGNORECASE)),
    ("pragma: no cover", re.compile(r"\bpragma:\s*no\s+cover\b", re.IGNORECASE)),
)


def check_no_inline_suppressions(
    directories: Sequence[Path] = (SRC_PACKAGE_DIR, TESTS_DIR, TOOLS_DIR),
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for directory in directories:
        for path in iter_python_files(directory):
            content = path.read_text(encoding="utf-8")
            tokens = tokenize.generate_tokens(io.StringIO(content).readline)
            for token_type, token_text, (line, col), _, _ in tokens:
                if token_type != tokenize.COMMENT:
                    continue
                for label, pattern in _INLINE_SUPPRESSION_PATTERNS:
                    if pattern.search(token_text):
                        findings.append(
                            LintFinding(
                                rule="PL008",
                                path=path,
                                line=line,
                                col=col + 1,
                                message=(
                                    f"Inline suppression comment '{label}' is "
                                    "not allowed."
                                ),
                                hint=(
                                    "Fix the underlying lint, type, or coverage "
                                    "issue instead of using inline suppression "
                                    "comments."
                                ),
                            )
                        )
    return findings
