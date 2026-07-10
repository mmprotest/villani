from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class InterruptController:
    _active_interrupt_count: int = 0

    def register_interrupt(self) -> Literal["interrupt", "exit"]:
        self._active_interrupt_count += 1
        if self._active_interrupt_count >= 2:
            return "exit"
        return "interrupt"

    def reset_interrupt_state(self) -> None:
        self._active_interrupt_count = 0
