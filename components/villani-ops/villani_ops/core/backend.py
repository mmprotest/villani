from __future__ import annotations
import os
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

Provider = Literal["openai-compatible", "openai", "anthropic", "villani-code", "local", "custom"]
Role = Literal["coding", "classification", "review", "policy", "investigation", "selection"]
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

    def resolved_api_key(self) -> str | None:
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return self.api_key if self.api_key != "***REDACTED***" else None

    def api_key_configured(self) -> bool:
        if self.api_key not in {None, "", "***REDACTED***"}:
            return True
        if self.api_key_env:
            return bool(os.environ.get(self.api_key_env))
        return False

    def api_key_status(self) -> str:
        if self.api_key not in {None, "", "***REDACTED***"}:
            return "direct_key_configured"
        if self.api_key_env:
            return "env_var_present" if os.environ.get(self.api_key_env) else "env_var_missing"
        return "missing"

    def redacted_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if data.get("api_key"):
            data["api_key"] = "***REDACTED***"
        return data

    def estimate_cost(self, input_tokens:int=0, output_tokens:int=0) -> float:
        return (input_tokens/1_000_000*self.input_cost_per_million) + (output_tokens/1_000_000*self.output_cost_per_million)

def select_backend(backends: dict[str, Backend], role: str) -> Backend:
    eligible=[b for b in backends.values() if b.enabled and role in b.roles]
    if not eligible:
        raise ValueError(f"No enabled backend configured for role '{role}'.")
    return sorted(eligible, key=lambda b: (-b.capability_score, b.output_cost_per_million, b.input_cost_per_million, b.name))[0]

def coding_backends(backends: dict[str, Backend]) -> list[Backend]:
    return [b for b in backends.values() if b.enabled and "coding" in b.roles]
