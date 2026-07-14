"""The closed-loop controller's complete deterministic transition table."""

from __future__ import annotations

from .protocol import ControllerState


TERMINAL_STATES: frozenset[ControllerState] = frozenset(
    {"COMPLETED", "EXHAUSTED", "FAILED"}
)

ALLOWED_TRANSITIONS: dict[ControllerState, frozenset[ControllerState]] = {
    "CREATED": frozenset({"CLASSIFYING"}),
    "CLASSIFYING": frozenset({"CLASSIFIED", "FAILED"}),
    "CLASSIFIED": frozenset({"POLICY_SELECTED", "EXHAUSTED", "FAILED"}),
    "POLICY_SELECTED": frozenset(
        {"ATTEMPT_RUNNING", "SELECTING", "EXHAUSTED", "FAILED"}
    ),
    "ATTEMPT_RUNNING": frozenset({"ATTEMPT_COMPLETED", "FAILED"}),
    "ATTEMPT_COMPLETED": frozenset({"VERIFYING", "REJECTED", "FAILED"}),
    "VERIFYING": frozenset({"VERIFIED", "FAILED"}),
    "VERIFIED": frozenset({"SELECTING", "REJECTED", "FAILED"}),
    "REJECTED": frozenset({"POLICY_SELECTED", "ESCALATING", "EXHAUSTED", "FAILED"}),
    "ESCALATING": frozenset({"POLICY_SELECTED", "EXHAUSTED", "FAILED"}),
    "SELECTING": frozenset(
        {"AWAITING_APPROVAL", "MATERIALIZING", "EXHAUSTED", "FAILED"}
    ),
    "AWAITING_APPROVAL": frozenset({"MATERIALIZING", "COMPLETED", "FAILED"}),
    "MATERIALIZING": frozenset({"COMPLETED", "FAILED"}),
    "COMPLETED": frozenset(),
    "EXHAUSTED": frozenset(),
    "FAILED": frozenset(),
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
