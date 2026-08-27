from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClientOrderIdFactory:
    """Traceable v4.1 IDs; persistence of retry identity lives in the broker order row."""

    runtime_id: str
    sequence: int = 0

    def _next(self) -> int:
        self.sequence += 1
        return self.sequence

    def order(self, lane: str, symbol: str) -> str:
        return f"qg-v41-{lane.lower()}-{symbol.lower()}-{self.runtime_id}-{self._next()}"

    def stop(self, symbol: str) -> str:
        return f"qg-v41-stop-{symbol.lower()}-{self.runtime_id}-{self._next()}"

    @staticmethod
    def force_flat(symbol: str, episode_id: str, sequence: int) -> str:
        return f"qg-v41-force-flat-{symbol.lower()}-{episode_id}-{int(sequence)}"
