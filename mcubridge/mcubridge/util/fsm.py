"""FSM factory with McuBridge defaults."""

from __future__ import annotations

from typing import Any

from transitions import Machine


def create_fsm(
    model: Any,
    *,
    states: list[str | dict[str, Any]],
    transitions: list[dict[str, Any]],
    initial: str,
    after_state_change: str | None = None,
    auto_transitions: bool = True,
    ignore_invalid_triggers: bool = True,
    model_attribute: str = "fsm_state",
) -> Machine:
    """Create a ``transitions.Machine`` with standard daemon defaults.

    Every FSM in the daemon uses ``queued=True`` and most share
    ``model_attribute="fsm_state"`` plus ``ignore_invalid_triggers=True``.
    This helper captures those conventions so each call-site only
    declares states, transitions and the few overrides it needs.
    """
    kwargs: dict[str, Any] = {}
    if after_state_change is not None:
        kwargs["after_state_change"] = after_state_change

    return Machine(
        model=model,
        states=states,
        initial=initial,
        transitions=transitions,
        queued=True,
        model_attribute=model_attribute,
        ignore_invalid_triggers=ignore_invalid_triggers,
        auto_transitions=auto_transitions,
        **kwargs,
    )
