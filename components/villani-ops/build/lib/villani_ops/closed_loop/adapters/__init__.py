"""Real closed-loop adapters built from existing Villani Ops primitives."""

from .evidence_selector import EvidenceSelectorAdapter
from .patch_materializer import PatchMaterializerAdapter
from .villani_code_attempt import VillaniCodeAttemptAdapter
from .villani_verifier import VillaniVerifierAdapter

__all__ = [
    "EvidenceSelectorAdapter",
    "PatchMaterializerAdapter",
    "VillaniCodeAttemptAdapter",
    "VillaniVerifierAdapter",
]
