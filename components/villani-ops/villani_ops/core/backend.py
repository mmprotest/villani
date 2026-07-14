from __future__ import annotations
import os
from collections.abc import Mapping
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

Provider = Literal[
    "openai-compatible", "openai", "anthropic", "villani-code", "local", "custom"
]
Role = Literal[
    "coding", "classification", "review", "policy", "investigation", "selection"
]
BillingMode = Literal["token", "compute_time", "fixed", "hybrid", "unknown"]


class Backend(BaseModel):
    name: str
    provider: Provider
    base_url: str | None = None
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    billing_mode: BillingMode = "unknown"
    compute_cost_per_hour: float | None = Field(default=None, ge=0)
    fixed_cost_per_attempt: float | None = Field(default=None, ge=0)
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    estimated_output_tokens: int | None = Field(default=None, ge=0)
    estimated_duration_seconds: float | None = Field(default=None, ge=0)
    capability_score_source: str = Field(default="user_configured", min_length=1)
    roles: list[Role] = Field(default_factory=lambda: ["coding"])
    capability_score: int = 0
    max_parallel: int = Field(default=1, ge=1, le=32)
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)  # backward compatible
    command_name: str | None = None
    execution_environment: str | None = None

    @model_validator(mode="after")
    def normalize_provider_defaults(self) -> "Backend":
        # Keep legacy provider values parseable for compatibility commands. The
        # public closed loop validates its narrower vocabulary before execution.
        self.provider = str(self.provider).strip().lower().replace("_", "-")  # type: ignore[assignment]
        self.currency = self.currency.upper()
        if self.base_url:
            self.base_url = self.base_url.strip()
        if self.provider == "openai" and not str(self.base_url or "").strip():
            self.base_url = "https://api.openai.com/v1"
        return self

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_token_billing(cls, value: Any) -> Any:
        """Infer token billing only for old configs with a positive token price."""

        if not isinstance(value, dict) or "billing_mode" in value:
            return value
        input_price = value.get("input_cost_per_million")
        output_price = value.get("output_cost_per_million")

        def is_positive(price: Any) -> bool:
            if price is None or isinstance(price, bool):
                return False
            try:
                return float(price) > 0
            except (TypeError, ValueError):
                return False

        positive_price = any(
            is_positive(price) for price in (input_price, output_price)
        )
        return {**value, "billing_mode": "token" if positive_price else "unknown"}

    @staticmethod
    def _usable_direct_credential(value: str | None) -> bool:
        return bool(value and value.strip()) and value.strip() not in {
            "***REDACTED***",
            "[REDACTED]",
            "REDACTED",
        }

    def credential_reference_configured(self) -> bool:
        """Return whether configuration contains a usable credential reference.

        This is deliberately structural: it never reads the referenced
        environment variable. Direct credentials remain readable for legacy
        configurations, while setup-generated configuration uses ``api_key_env``.
        """

        return self._usable_direct_credential(self.api_key) or bool(
            self.api_key_env and self.api_key_env.strip()
        )

    def resolved_api_key(self, environ: Mapping[str, str] | None = None) -> str | None:
        values = os.environ if environ is None else environ
        if self.api_key_env and self.api_key_env.strip():
            value = values.get(self.api_key_env.strip())
            return value if value and value.strip() else None
        return self.api_key if self._usable_direct_credential(self.api_key) else None

    def runtime_credential_available(
        self, environ: Mapping[str, str] | None = None
    ) -> bool:
        return self.resolved_api_key(environ) is not None

    def require_runtime_credential(
        self, environ: Mapping[str, str] | None = None
    ) -> str:
        value = self.resolved_api_key(environ)
        if value is not None:
            return value
        if self.api_key_env and self.api_key_env.strip():
            raise ValueError(
                f"backend {self.name!r} credential environment variable "
                f"{self.api_key_env.strip()} is missing or empty"
            )
        raise ValueError(f"backend {self.name!r} has no usable credential reference")

    def api_key_configured(self) -> bool:
        """Backward-compatible alias for structural configuration checks."""

        return self.credential_reference_configured()

    def api_key_status(self) -> str:
        if self._usable_direct_credential(self.api_key):
            return "direct_key_configured"
        if self.api_key_env and self.api_key_env.strip():
            return (
                "env_var_present"
                if self.runtime_credential_available()
                else "env_var_missing"
            )
        return "missing"

    def redacted_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if data.get("api_key"):
            data["api_key"] = "***REDACTED***"
        return data

    def estimate_cost(self, input_tokens: int = 0, output_tokens: int = 0) -> float:
        return (input_tokens / 1_000_000 * self.input_cost_per_million) + (
            output_tokens / 1_000_000 * self.output_cost_per_million
        )


def select_backend(backends: dict[str, Backend], role: str) -> Backend:
    eligible = [b for b in backends.values() if b.enabled and role in b.roles]
    if not eligible:
        raise ValueError(f"No enabled backend configured for role '{role}'.")
    return sorted(
        eligible,
        key=lambda b: (
            -b.capability_score,
            b.output_cost_per_million,
            b.input_cost_per_million,
            b.name,
        ),
    )[0]


def coding_backends(backends: dict[str, Backend]) -> list[Backend]:
    return [b for b in backends.values() if b.enabled and "coding" in b.roles]
