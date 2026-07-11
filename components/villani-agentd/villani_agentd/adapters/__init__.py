"""Observation adapters for machine-readable local agent streams."""

from .contract import AdapterContext, DetectionResult, SensitiveFieldPolicy
from .implementations import ADAPTERS, get_adapter

__all__ = ["ADAPTERS", "AdapterContext", "DetectionResult", "SensitiveFieldPolicy", "get_adapter"]
