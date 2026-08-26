from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quietgrid_v40.frozen import Frozen31111, load_frozen_31111


@dataclass(frozen=True)
class Frozen31111Runtime:
    """Runtime view of the attested 31111 artifact; no mutable strategy copy."""

    frozen: Frozen31111

    @classmethod
    def load(cls, repo_root: str | Path = ".") -> "Frozen31111Runtime":
        return cls(load_frozen_31111(repo_root))

    @property
    def candidate_id(self) -> str:
        return self.frozen.candidate_id

    @property
    def candidate_sha(self) -> str:
        return self.frozen.candidate_sha

    @property
    def economic_leverage(self) -> int:
        return self.frozen.economic_leverage

    @property
    def exposure_cutoff(self) -> str:
        return self.frozen.exposure_cutoff

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.frozen.symbols

    def symbol_profile(self, symbol: str) -> dict[str, Any]:
        profile = self.frozen.candidate["symbol_universe"].get(symbol.upper())
        if profile is None:
            raise KeyError(symbol)
        return dict(profile)

    def strategy_summary(self) -> dict[str, Any]:
        combination = self.frozen.candidate.get("combination_definition", {})
        return {
            "candidate_id": self.candidate_id,
            "candidate_sha": self.candidate_sha,
            "combination_id": combination.get("combination_id"),
            "range": combination.get("range"),
            "grid": combination.get("grid"),
            "profit": combination.get("profit"),
            "inventory": combination.get("inventory"),
            "stop": combination.get("stop"),
            "direction": self.frozen.candidate.get("direction"),
            "economic_leverage": self.economic_leverage,
            "exposure_cutoff": self.exposure_cutoff,
        }
