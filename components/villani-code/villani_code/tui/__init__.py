from __future__ import annotations

from typing import Any

__all__ = ["VillaniTUI"]


def __getattr__(name: str) -> Any:
    if name != "VillaniTUI":
        raise AttributeError(name)
    from villani_code.tui.app import VillaniTUI

    return VillaniTUI
