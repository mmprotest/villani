"""Shared contracts and process mechanics for read-only CLI roles."""

from .models import (
    CLI_CLASSIFIER_RESULT_SCHEMA_VERSION,
    CLI_SELECTOR_RESULT_SCHEMA_VERSION,
    CliClassifierResult,
    CliRoleFailure,
    CliSelectorResult,
    normalize_cli_classifier_result,
    normalize_cli_selector_result,
)
from .runtime import CliRoleExecution, execute_cli_role

__all__ = [
    "CLI_CLASSIFIER_RESULT_SCHEMA_VERSION",
    "CLI_SELECTOR_RESULT_SCHEMA_VERSION",
    "CliClassifierResult",
    "CliRoleExecution",
    "CliRoleFailure",
    "CliSelectorResult",
    "execute_cli_role",
    "normalize_cli_classifier_result",
    "normalize_cli_selector_result",
]
