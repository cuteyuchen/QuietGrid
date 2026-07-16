from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.models import GridState


ALLOWED_TRANSITIONS: dict[GridState, set[GridState]] = {
    GridState.IDLE: {GridState.SELECTING, GridState.OBSERVING, GridState.RECOVERING, GridState.STOPPED},
    GridState.SELECTING: {GridState.OBSERVING, GridState.STOPPED},
    GridState.OBSERVING: {GridState.READY, GridState.RUNNING, GridState.CLOSING, GridState.STOPPED},
    GridState.READY: {GridState.RUNNING, GridState.CLOSING, GridState.STOPPED},
    GridState.RUNNING: {
        GridState.REBALANCING,
        GridState.PAUSED,
        GridState.COOLDOWN,
        GridState.CLOSING,
        GridState.STOPPED,
    },
    GridState.REBALANCING: {GridState.RUNNING, GridState.COOLDOWN, GridState.CLOSING, GridState.STOPPED},
    GridState.RECOVERING: {GridState.RUNNING, GridState.CLOSING, GridState.STOPPED},
    GridState.PAUSED: {GridState.RUNNING, GridState.CLOSING, GridState.STOPPED},
    GridState.COOLDOWN: {GridState.OBSERVING, GridState.CLOSING, GridState.STOPPED},
    GridState.CLOSING: {GridState.STOPPED},
    GridState.STOPPED: {GridState.IDLE, GridState.OBSERVING},
}


class InvalidTransition(ValueError):
    pass


@dataclass
class TransitionRecord:
    symbol: str
    from_state: GridState
    to_state: GridState
    trigger: str
    detail: str | None
    at: datetime


@dataclass
class StateMachine:
    states: dict[str, GridState] = field(default_factory=dict)
    history: list[TransitionRecord] = field(default_factory=list)

    def get_state(self, symbol: str) -> GridState:
        return self.states.get(symbol, GridState.IDLE)

    def transition(
        self,
        symbol: str,
        to_state: GridState,
        trigger: str,
        detail: str | None = None,
        at: datetime | None = None,
    ) -> GridState:
        from_state = self.get_state(symbol)
        if to_state not in ALLOWED_TRANSITIONS[from_state]:
            raise InvalidTransition(f"{symbol}: {from_state.value} -> {to_state.value} 不允许。")

        self.states[symbol] = to_state
        self.history.append(
            TransitionRecord(
                symbol=symbol,
                from_state=from_state,
                to_state=to_state,
                trigger=trigger,
                detail=detail,
                at=at or datetime.now(timezone.utc),
            )
        )
        return to_state
