"""Recommendation-only routing artifacts.

Nothing in this package implements the controller ``PolicyEngine`` protocol.  Its
outputs are evidence artifacts and cannot be used as controller decisions.
"""

from .catalog import capability_catalog_snapshot
from .features import extract_task_features
from .models import CapabilityCatalogSnapshot, ShadowRecommendation, TaskFeatures
from .router import ShadowRouter

__all__ = [
    "CapabilityCatalogSnapshot",
    "ShadowRecommendation",
    "ShadowRouter",
    "TaskFeatures",
    "capability_catalog_snapshot",
    "extract_task_features",
]
