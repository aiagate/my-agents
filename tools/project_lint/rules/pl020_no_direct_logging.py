from ast import Attribute, Call, Import, ImportFrom, Name, walk
from pathlib import Path

from tools.project_lint.common import (
    SRC_PACKAGE_DIR,
    LintFinding,
    iter_python_files,
    parse_file,
    resolve_import_targets,
)

_FORBIDDEN_GLOBAL_LOGGING_APIS = frozenset(
    {
        "config",
        "getLogger",
        "basicConfig",
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "critical",
        "fatal",
        "exception",
        "log",
        "disable",
        "shutdown",
        "setLoggerClass",
        "setLogRecordFactory",
    }
)


def _is_in_infrastructure(path: Path, src_dir: Path) -> bool:
    rel = path.relative_to(src_dir)
    return len(rel.parts) >= 2 and rel.parts[0] == "infrastructure"


def _is_logging_config_module(module_name: str) -> bool:
    return module_name == "logging.config" or module_name.startswith("logging.config.")


def _is_super_call(expr: object) -> bool:
    return (
        isinstance(expr, Call)
        and isinstance(expr.func, Name)
        and expr.func.id == "super"
    )


def check_no_direct_logging(src_dir: Path = SRC_PACKAGE_DIR) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path in iter_python_files(src_dir):
        tree = parse_file(path)
        if not _is_in_infrastructure(path, src_dir):
            for node in walk(tree):
                if not isinstance(node, Import | ImportFrom):
                    continue
                for target in resolve_import_targets(node, path, src_dir):
                    if target == "logging" or target.startswith("logging."):
                        findings.append(
                            LintFinding(
                                rule="PL020",
                                path=path,
                                line=node.lineno,
                                col=node.col_offset + 1,
                                message=(
                                    "Module outside 'infrastructure' must not import "
                                    f"'{target}' directly."
                                ),
                                hint=(
                                    "Inject and call 'IAppLogger' from "
                                    "'my_agents.contracts.ports.logger' instead of "
                                    "importing Python's 'logging' module directly."
                                ),
                            )
                        )
            continue

        uses_logging = False
        logging_aliases: set[str] = set()
        for node in walk(tree):
            if isinstance(node, Import):
                for alias in node.names:
                    if alias.name == "logging":
                        uses_logging = True
                        logging_aliases.add(alias.asname or "logging")
                    elif _is_logging_config_module(alias.name):
                        uses_logging = True
                        findings.append(
                            LintFinding(
                                rule="PL020",
                                path=path,
                                line=node.lineno,
                                col=node.col_offset + 1,
                                message=(
                                    "Infrastructure module must not import global "
                                    f"logging configuration module '{alias.name}'."
                                ),
                                hint=(
                                    "Instantiate a private handler directly without "
                                    "configuring process-wide logging."
                                ),
                            )
                        )
                    elif alias.name.startswith("logging."):
                        uses_logging = True
                        if alias.asname is None:
                            logging_aliases.add("logging")
            elif (
                isinstance(node, ImportFrom)
                and node.level == 0
                and node.module is not None
            ):
                if _is_logging_config_module(node.module):
                    uses_logging = True
                    findings.append(
                        LintFinding(
                            rule="PL020",
                            path=path,
                            line=node.lineno,
                            col=node.col_offset + 1,
                            message=(
                                "Infrastructure module must not import global "
                                f"logging configuration module '{node.module}'."
                            ),
                            hint=(
                                "Instantiate a private handler directly without "
                                "configuring process-wide logging."
                            ),
                        )
                    )
                elif node.module == "logging" or node.module.startswith("logging."):
                    uses_logging = True
                    for alias in node.names:
                        if alias.name == "*":
                            findings.append(
                                LintFinding(
                                    rule="PL020",
                                    path=path,
                                    line=node.lineno,
                                    col=node.col_offset + 1,
                                    message=(
                                        "Infrastructure module must not wildcard-import "
                                        f"from '{node.module}'."
                                    ),
                                    hint=(
                                        "Import only explicit non-global logging "
                                        "symbols such as 'Formatter', 'LogRecord', "
                                        "or 'RotatingFileHandler'."
                                    ),
                                )
                            )
                        elif (
                            node.module == "logging"
                            and alias.name in _FORBIDDEN_GLOBAL_LOGGING_APIS
                        ):
                            findings.append(
                                LintFinding(
                                    rule="PL020",
                                    path=path,
                                    line=node.lineno,
                                    col=node.col_offset + 1,
                                    message=(
                                        "Infrastructure module must not import global "
                                        f"logging API 'logging.{alias.name}'."
                                    ),
                                    hint=(
                                        "Instantiate a private handler directly without "
                                        "using global logging registries or root logger "
                                        "functions."
                                    ),
                                )
                            )

        if not uses_logging:
            continue

        for node in walk(tree):
            if (
                logging_aliases
                and isinstance(node, Attribute)
                and isinstance(node.value, Name)
                and node.value.id in logging_aliases
                and node.attr in _FORBIDDEN_GLOBAL_LOGGING_APIS
            ):
                findings.append(
                    LintFinding(
                        rule="PL020",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message=(
                            "Infrastructure module must not use global logging "
                            f"API '{node.value.id}.{node.attr}'."
                        ),
                        hint=(
                            "Instantiate a private handler directly without using "
                            "global logging registries or root logger functions."
                        ),
                    )
                )
            elif (
                isinstance(node, Call)
                and isinstance(node.func, Attribute)
                and node.func.attr == "emit"
                and not _is_super_call(node.func.value)
            ):
                findings.append(
                    LintFinding(
                        rule="PL020",
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        message=(
                            "Infrastructure logging module must not call '.emit()' "
                            "directly."
                        ),
                        hint=(
                            "Call 'handler.handle(record)' instead of "
                            "'handler.emit(record)' so the handler lock is acquired; "
                            "reserve direct 'emit()' calls for 'super().emit(record)' "
                            "inside a Handler subclass."
                        ),
                    )
                )

    return findings
