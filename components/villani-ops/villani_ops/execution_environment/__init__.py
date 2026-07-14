"""Execution-environment discovery and local provider APIs."""

from .inspection import inspect_repository, lockfile_digests
from .container import ContainerProvider
from .devcontainer import DevcontainerProvider
from .models import ExecutionEnvironmentConfig, PreparedEnvironment, SetupLimits
from .secrets import LocalSecretBroker, SecretBroker, SecretLease
from .security import ExecutionPolicyDenied
from .providers import (
    ExecutionEnvironmentProvider,
    InheritProvider,
    SetupCommandProvider,
    preflight_report,
    provider_from_configuration,
)
from .validation_discovery import (
    CONFIRMATION_THRESHOLD,
    ValidationDiscoveryPlugin,
    ValidationDiscoveryRegistry,
    confirmed_command,
    discover_repository_validation,
    display_argv,
    parse_manual_command,
)

__all__ = [
    "ExecutionEnvironmentConfig",
    "ExecutionEnvironmentProvider",
    "ExecutionPolicyDenied",
    "InheritProvider",
    "PreparedEnvironment",
    "ContainerProvider",
    "DevcontainerProvider",
    "LocalSecretBroker",
    "SecretBroker",
    "SecretLease",
    "SetupCommandProvider",
    "SetupLimits",
    "inspect_repository",
    "lockfile_digests",
    "preflight_report",
    "provider_from_configuration",
    "CONFIRMATION_THRESHOLD",
    "ValidationDiscoveryPlugin",
    "ValidationDiscoveryRegistry",
    "confirmed_command",
    "discover_repository_validation",
    "display_argv",
    "parse_manual_command",
]
