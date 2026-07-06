from __future__ import annotations

from core.models import GridState
from strategy.state_machine import InvalidTransition, StateMachine


def test_state_machine_records_valid_transitions() -> None:
    sm = StateMachine()

    assert sm.transition("AAPLUSDT", GridState.OBSERVING, "window_open") == GridState.OBSERVING
    assert sm.transition("AAPLUSDT", GridState.RUNNING, "grid_ready") == GridState.RUNNING

    assert sm.get_state("AAPLUSDT") == GridState.RUNNING
    assert len(sm.history) == 2
    assert sm.history[-1].trigger == "grid_ready"


def test_state_machine_rejects_invalid_transition() -> None:
    sm = StateMachine()

    try:
        sm.transition("AAPLUSDT", GridState.RUNNING, "skip_observation")
    except InvalidTransition as exc:
        assert "不允许" in str(exc)
    else:
        raise AssertionError("invalid transition should be rejected")

