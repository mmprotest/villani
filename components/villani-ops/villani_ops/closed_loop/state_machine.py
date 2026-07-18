"""The closed-loop controller's complete deterministic transition table."""

from __future__ import annotations

from .protocol import ControllerState


TERMINAL_STATES: frozenset[ControllerState] = frozenset(
    {"COMPLETED", "EXHAUSTED", "FAILED", "CANCELLED"}
)

ALLOWED_TRANSITIONS: dict[ControllerState, frozenset[ControllerState]] = {
    "CREATED": frozenset({"CLASSIFYING", "CANCELLED"}),
    "CLASSIFYING": frozenset({"CLASSIFIED", "FAILED", "CANCELLED"}),
    "CLASSIFIED": frozenset({"POLICY_SELECTED", "EXHAUSTED", "FAILED", "CANCELLED"}),
    "POLICY_SELECTED": frozenset(
        {"ATTEMPT_RUNNING", "VERIFYING", "SELECTING", "EXHAUSTED", "FAILED", "CANCELLED"}
    ),
    "ATTEMPT_RUNNING": frozenset({"ATTEMPT_COMPLETED", "FAILED", "CANCELLED"}),
    "ATTEMPT_COMPLETED": frozenset({"VERIFYING", "REJECTED", "FAILED", "CANCELLED"}),
    "VERIFYING": frozenset({"VERIFIED", "FAILED", "CANCELLED"}),
    "VERIFIED": frozenset({"SELECTING", "REJECTED", "FAILED", "CANCELLED"}),
    "REJECTED": frozenset({"POLICY_SELECTED", "ESCALATING", "EXHAUSTED", "FAILED", "CANCELLED"}),
    "ESCALATING": frozenset({"POLICY_SELECTED", "EXHAUSTED", "FAILED", "CANCELLED"}),
    "SELECTING": frozenset(
        {"AWAITING_APPROVAL", "MATERIALIZING", "EXHAUSTED", "FAILED", "CANCELLED"}
    ),
    "AWAITING_APPROVAL": frozenset({"MATERIALIZING", "COMPLETED", "FAILED", "CANCELLED"}),
    "MATERIALIZING": frozenset({"COMPLETED", "FAILED"}),
    "COMPLETED": frozenset(),
    "EXHAUSTED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


class StateTransitionError(RuntimeError):
    """Base error for a forbidden controller transition."""


class IllegalTransitionError(StateTransitionError):
    """Raised when an edge is absent from the canonical transition table."""


class TerminalStateTransitionError(StateTransitionError):
    """Raised for every attempted transition out of a terminal state."""


class ClosedLoopStateMachine:
    """Small state holder that accepts only canonical, explicit transitions."""

    def __init__(self, initial_state: ControllerState = "CREATED") -> None:
        if initial_state not in ALLOWED_TRANSITIONS:
            raise ValueError(f"unknown controller state: {initial_state}")
        self._state = initial_state

    @property
    def state(self) -> ControllerState:
        return self._state

    @property
    def terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    def require_transition(self, target: ControllerState) -> None:
        if self.terminal:
            raise TerminalStateTransitionError(
                f"terminal state {self._state} cannot transition to {target}"
            )
        if target not in ALLOWED_TRANSITIONS[self._state]:
            raise IllegalTransitionError(
                f"illegal controller transition: {self._state} -> {target}"
            )

    def transition(self, target: ControllerState) -> ControllerState:
        self.require_transition(target)
        previous = self._state
        self._state = target
        return previous
