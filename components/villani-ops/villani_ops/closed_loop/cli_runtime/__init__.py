"""Provider-neutral, shell-free runtime for one external CLI invocation.

The package deliberately contains no command construction, role prompts, or
provider response interpretation.  Public imports are lazy so the canonical
schema registry can import the record models without creating an import cycle.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "CliCancellationHandle": ".cancellation",
    "CliCancellationOrigin": ".models",
    "CliEnvironmentPolicy": ".environment",
    "CliEnvironmentVariable": ".models",
    "CliFailure": ".models",
    "CliFailureDetail": ".models",
    "CliInvocation": ".models",
    "CliInvocationRecord": ".models",
    "minimal_cli_environment_values": ".environment",
    "CliOutputLimits": ".models",
    "CliOutputTail": ".models",
    "CLI_INFRASTRUCTURE_FAILURE_SCHEMA_VERSION": ".failure_presentation",
    "CliInfrastructureFailurePresentation": ".failure_presentation",
    "build_cli_failure_presentation": ".failure_presentation",
    "write_cli_failure_presentation": ".failure_presentation",
    "CliProcessResult": ".models",
    "CliProcessSupervisor": ".supervisor",
    "CliRawEvent": ".models",
    "CliStreamResult": ".models",
    "ResolvedCliEnvironment": ".environment",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


__all__ = sorted(_EXPORTS)
