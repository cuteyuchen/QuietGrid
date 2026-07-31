"""Immutable v2.8 combinatorial research definitions.

This module deliberately contains no performance-dependent selection.  It
defines the registered factor mapping, the deterministic Phase-1 covering
array, and the Hamming-neighbour relation used by the later reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from itertools import combinations, product
from random import Random
from typing import Any, Iterable
import statistics


FACTOR_LEVELS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("A", (1, 2, 3, 4)),
    ("B", (1, 2, 3, 4)),
    ("C", (1, 2, 3, 4)),
    ("D", (1, 2, 3, 4)),
    ("E", (1, 2, 3, 4, 5)),
)
PHASE1_SEED = 20260801
PHASE1_TARGET_SIZE = 96
ANCHOR_IDS: tuple[str, ...] = (
    "11111", "12111", "13111", "14111", "21111", "31111", "41111",
    "11211", "11311", "11411", "11121", "11131", "11141", "11112",
    "11113", "11114", "11115", "22333", "22434", "31445", "32333",
    "42335", "43445",
)


@dataclass(frozen=True, order=True)
class Combination:
    """A registered A/B/C/D/E parameter tuple."""

    a: int
    b: int
    c: int
    d: int
    e: int

    def __post_init__(self) -> None:
        for value, (_name, levels) in zip(self.values, FACTOR_LEVELS):
            if value not in levels:
                raise ValueError(f"非法组合等级: {self.id}")

    @property
    def values(self) -> tuple[int, int, int, int, int]:
        return (self.a, self.b, self.c, self.d, self.e)

    @property
    def id(self) -> str:
        return "".join(str(value) for value in self.values)

    @classmethod
    def parse(cls, value: str) -> "Combination":
        raw = str(value).strip()
        if len(raw) != 5 or not raw.isdigit():
            raise ValueError("组合 ID 必须是五位数字。")
        return cls(*(int(item) for item in raw))

    def with_direction(self, direction: str) -> str:
        direction = str(direction).upper()
        if direction not in {"N", "L"}:
            raise ValueError("方向后缀必须是 N 或 L。")
        return f"{self.id}-{direction}"


def all_combinations() -> tuple[Combination, ...]:
    return tuple(Combination(*values) for values in product(*(levels for _, levels in FACTOR_LEVELS)))


def registered_anchors() -> tuple[Combination, ...]:
    return tuple(Combination.parse(value) for value in ANCHOR_IDS)


def _pair_keys(combination: Combination) -> tuple[tuple[int, int, int, int], ...]:
    return tuple(
        (left, right, combination.values[left], combination.values[right])
        for left, right in combinations(range(5), 2)
    )


def pairwise_coverage(items: Iterable[Combination]) -> dict[tuple[int, int, int, int], int]:
    counts: dict[tuple[int, int, int, int], int] = {}
    for combination in items:
        for key in _pair_keys(combination):
            counts[key] = counts.get(key, 0) + 1
    return counts


def pairwise_audit(items: Iterable[Combination], minimum: int = 2) -> dict[str, Any]:
    combinations_ = tuple(items)
    counts = pairwise_coverage(combinations_)
    expected = [
        (left, right, left_value, right_value)
        for left, right in combinations(range(5), 2)
        for left_value in FACTOR_LEVELS[left][1]
        for right_value in FACTOR_LEVELS[right][1]
    ]
    missing = [key for key in expected if counts.get(key, 0) < minimum]
    return {
        "combination_count": len(combinations_),
        "minimum_required": minimum,
        "minimum_actual": min((counts.get(key, 0) for key in expected), default=0),
        "passed": not missing,
        "undercovered": [
            {"factors": f"{FACTOR_LEVELS[left][0]}x{FACTOR_LEVELS[right][0]}", "levels": f"{lv}/{rv}", "count": counts.get((left, right, lv, rv), 0)}
            for left, right, lv, rv in missing
        ],
    }


def generate_phase1_covering_array(
    *, target_size: int = PHASE1_TARGET_SIZE, seed: int = PHASE1_SEED
) -> tuple[Combination, ...]:
    """Generate the pre-registered deterministic 96-row pairwise array.

    Greedy selection maximises currently uncovered pair slots, then minimises
    per-factor level imbalance.  All tie-breaking is seeded and independent of
    outcomes, so the list can be frozen before any backtest executes.
    """
    if target_size < len(ANCHOR_IDS):
        raise ValueError("目标组合数不能少于全部锚点组合。")
    rng = Random(seed)
    pool = list(all_combinations())
    selected = list(registered_anchors())
    selected_ids = {item.id for item in selected}
    counts = pairwise_coverage(selected)
    level_counts = [{level: 0 for level in levels} for _name, levels in FACTOR_LEVELS]
    for item in selected:
        for index, value in enumerate(item.values):
            level_counts[index][value] += 1
    tie = {item.id: rng.random() for item in pool}

    while len(selected) < target_size:
        candidates = [item for item in pool if item.id not in selected_ids]
        def score(item: Combination) -> tuple[int, int, float]:
            coverage_gain = sum(max(0, 2 - counts.get(key, 0)) for key in _pair_keys(item))
            imbalance = sum(level_counts[index][value] for index, value in enumerate(item.values))
            return (coverage_gain, -imbalance, tie[item.id])
        best = max(candidates, key=score)
        selected.append(best)
        selected_ids.add(best.id)
        for key in _pair_keys(best):
            counts[key] = counts.get(key, 0) + 1
        for index, value in enumerate(best.values):
            level_counts[index][value] += 1

    audit = pairwise_audit(selected)
    if not audit["passed"]:
        raise RuntimeError("固定 covering array 未达到两两交互至少两次覆盖。")
    return tuple(selected)


def catalog_sha256(items: Iterable[Combination]) -> str:
    payload = "\n".join(item.id for item in items).encode("ascii")
    return sha256(payload).hexdigest()


def neighbors(item: Combination, *, available: Iterable[Combination] | None = None) -> tuple[Combination, ...]:
    available_ids = {candidate.id for candidate in available} if available is not None else None
    result: list[Combination] = []
    for index, (_name, levels) in enumerate(FACTOR_LEVELS):
        value = item.values[index]
        for candidate_value in (value - 1, value + 1):
            if candidate_value not in levels:
                continue
            values = list(item.values)
            values[index] = candidate_value
            candidate = Combination(*values)
            if available_ids is None or candidate.id in available_ids:
                result.append(candidate)
    return tuple(sorted(result))


def factor_snapshot(item: Combination) -> dict[str, Any]:
    """Pre-registered numeric controls consumed by the v2.8 runner."""
    range_multiplier = {1: 1.0, 2: 1.5, 3: 2.0, 4: None}[item.a]
    grid = {
        1: {"min_grid_num": 5, "max_grid_num": 10, "step_multiplier": 1.5},
        2: {"min_grid_num": 10, "max_grid_num": 20, "step_multiplier": 1.0},
        3: {"min_grid_num": 20, "max_grid_num": 50, "minimum_step_pct": 0.0008},
        4: {"min_grid_num": 50, "max_grid_num": 100, "minimum_step_pct": 0.0005},
    }[item.b]
    profits = {
        1: {"mode": "NONE"},
        2: {"mode": "FIXED", "activation_pct": 0.01},
        3: {"mode": "PEAK", "activation_pct": 0.01, "suppress": 0.20, "reduce": 0.35, "close": 0.50, "locked": 0.25},
        4: {"mode": "INVENTORY_AWARE", "activation_pct": 0.005, "suppress_drag": 0.25, "reduce_drag": 0.40, "close_drag": 0.60, "peak_close": 0.40},
    }[item.c]
    inventory = {
        1: {"caution": 0.40, "reduce_only": 0.80},
        2: {"caution": 0.35, "reduce_only": 0.80},
        3: {"caution": 0.35, "reduce_only": 0.50},
        4: {"caution": 0.25, "reduce_only": 0.40, "close_drag": 0.60},
    }[item.d]
    stop = {
        1: {"mode": "BASELINE", "atr_buffer": 0.0},
        2: {"mode": "HALF_ATR", "atr_buffer": 0.5},
        3: {"mode": "ONE_ATR", "atr_buffer": 1.0},
        4: {"mode": "TWO_ATR", "atr_buffer": 2.0},
        5: {"mode": "TIME_CONFIRMED", "atr_buffer": 1.0, "confirm_bars": 30},
    }[item.e]
    return {
        "combination_id": item.id,
        "range": {"level": item.a, "multiplier": range_multiplier, "adaptive": item.a == 4},
        "grid": {"level": item.b, **grid},
        "profit": {"level": item.c, **profits},
        "inventory": {"level": item.d, **inventory},
        "stop": {"level": item.e, **stop},
    }


def phase1_profile_summaries(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate Phase 1 with seeds averaged inside each market window first."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(
            (
                str(row["combination_id"]),
                str(row["direction"]),
                str(row["scenario"]),
            ),
            [],
        ).append(row)
    summaries: list[dict[str, Any]] = []
    for (combination_id, direction, scenario), profile_rows in sorted(grouped.items()):
        windows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in profile_rows:
            windows.setdefault(
                (str(row["symbol"]), str(row["window_key"])), []
            ).append(row)
        window_rows: list[dict[str, float]] = []
        for seed_rows in windows.values():
            window_rows.append(
                {
                    "net_pnl": statistics.fmean(float(row["net_pnl"]) for row in seed_rows),
                    "paired_grid_pnl": statistics.fmean(
                        float(row["paired_grid_pnl"]) for row in seed_rows
                    ),
                    "inventory_drag": statistics.fmean(
                        float(row["inventory_drag"]) for row in seed_rows
                    ),
                }
            )
        pnls = [row["net_pnl"] for row in window_rows]
        gains = sum(max(value, 0.0) for value in pnls)
        losses = sum(max(-value, 0.0) for value in pnls)
        paired = sum(row["paired_grid_pnl"] for row in window_rows)
        drag = sum(row["inventory_drag"] for row in window_rows)
        summaries.append(
            {
                "combination_id": combination_id,
                "direction": direction,
                "scenario": scenario,
                "window_count": len(window_rows),
                "total_pnl": sum(pnls),
                "median_window_pnl": statistics.median(pnls),
                "profit_factor": gains / losses if losses else (gains if gains else 0.0),
                "positive_window_ratio": sum(value > 0 for value in pnls) / len(pnls),
                "inventory_drag_ratio": drag / max(paired, 0.01),
                "best_window_concentration": max((max(value, 0.0) for value in pnls), default=0.0)
                / max(gains, 0.01),
            }
        )
    return summaries


def exposure_time_splits(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """Split unique force-close timestamps without separating concurrent symbols."""
    rows = tuple(rows)
    times = sorted(
        {str(row["force_close_at"]) for row in rows},
        key=datetime.fromisoformat,
    )
    if not times:
        return {}
    cut = max(1, int(len(times) * 0.60))
    early_times = set(times[:cut])
    return {
        (str(row["symbol"]), str(row["window_key"])): (
            "EXPOSED_EARLY"
            if str(row["force_close_at"]) in early_times
            else "EXPOSED_LATE"
        )
        for row in rows
    }


def exposed_early_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(rows)
    splits = exposure_time_splits(rows)
    return [
        row
        for row in rows
        if splits[(str(row["symbol"]), str(row["window_key"]))] == "EXPOSED_EARLY"
    ]


def select_phase2_profiles(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the registered Phase 2 gate on EXPOSED_EARLY only."""
    summaries = phase1_profile_summaries(exposed_early_rows(rows))
    indexed = {
        (row["combination_id"], row["direction"], row["scenario"]): row
        for row in summaries
    }
    profiles = {
        (row["combination_id"], row["direction"])
        for row in summaries
    }
    profiles.update((anchor, direction) for anchor in ANCHOR_IDS for direction in ("NEUTRAL", "LONG"))
    selected: list[dict[str, Any]] = []
    for combination_id, direction in sorted(profiles):
        primary = indexed.get((combination_id, direction, "PRIMARY_ZERO_MAKER"))
        stress = indexed.get((combination_id, direction, "EXECUTION_STRESS"))
        passed = bool(
            primary
            and stress
            and primary["total_pnl"] > 0
            and primary["median_window_pnl"] > 0
            and stress["total_pnl"] >= 0
            and primary["profit_factor"] >= 1.05
            and primary["positive_window_ratio"] >= 0.50
            and primary["inventory_drag_ratio"] <= 0.75
            and primary["best_window_concentration"] <= 0.50
        )
        anchor = combination_id in ANCHOR_IDS
        if not (passed or anchor):
            continue
        selected.append(
            {
                "combination_id": combination_id,
                "direction": direction,
                "selection_reason": "ANCHOR_CONTROL" if anchor else "PHASE1_GATE_PASS",
                "phase1_gate_passed": passed,
                "primary_total_pnl": primary["total_pnl"] if primary else None,
                "primary_median_window_pnl": primary["median_window_pnl"] if primary else None,
                "stress_total_pnl": stress["total_pnl"] if stress else None,
                "profit_factor": primary["profit_factor"] if primary else None,
                "positive_window_ratio": primary["positive_window_ratio"] if primary else None,
                "inventory_drag_ratio": primary["inventory_drag_ratio"] if primary else None,
                "best_window_concentration": primary["best_window_concentration"] if primary else None,
            }
        )
    return selected


def combination_neighborhood_rows(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Calculate the registered adjacent-level stability checks from R1 rows."""
    summaries = phase1_profile_summaries(rows)
    indexed = {
        (row["combination_id"], row["direction"], row["scenario"]): row
        for row in summaries
    }
    profiles = sorted({(row["combination_id"], row["direction"]) for row in summaries})
    available = {
        direction: tuple(
            Combination.parse(combination_id)
            for combination_id, candidate_direction in profiles
            if candidate_direction == direction
        )
        for _combination_id, direction in profiles
    }
    output: list[dict[str, Any]] = []
    for combination_id, direction in profiles:
        item = Combination.parse(combination_id)
        adjacent = neighbors(item, available=available[direction])
        primary_neighbors = [
            indexed[(neighbor.id, direction, "PRIMARY_ZERO_MAKER")]
            for neighbor in adjacent
            if (neighbor.id, direction, "PRIMARY_ZERO_MAKER") in indexed
        ]
        stress_neighbors = [
            indexed[(neighbor.id, direction, "EXECUTION_STRESS")]
            for neighbor in adjacent
            if (neighbor.id, direction, "EXECUTION_STRESS") in indexed
        ]
        primary = indexed.get((combination_id, direction, "PRIMARY_ZERO_MAKER"))
        primary_totals = [row["total_pnl"] for row in primary_neighbors]
        median_neighbor = statistics.median(primary_totals) if primary_totals else 0.0
        positive_ratio = (
            sum(row["total_pnl"] > 0 for row in primary_neighbors)
            / len(primary_neighbors)
            if primary_neighbors
            else 0.0
        )
        stress_ratio = (
            sum(row["total_pnl"] >= 0 for row in stress_neighbors)
            / len(stress_neighbors)
            if stress_neighbors
            else 0.0
        )
        candidate_total = primary["total_pnl"] if primary else 0.0
        stable = bool(
            primary_neighbors
            and positive_ratio >= 0.50
            and stress_ratio >= 0.40
            and median_neighbor > 0
            and candidate_total <= median_neighbor * 5
        )
        output.append(
            {
                "combination_id": combination_id,
                "direction": direction,
                "available_neighbor_count": len(primary_neighbors),
                "neighbor_ids": ";".join(neighbor.id for neighbor in adjacent),
                "neighbor_positive_ratio": positive_ratio,
                "neighbor_stress_nonnegative_ratio": stress_ratio,
                "neighbor_median_net_pnl": median_neighbor,
                "candidate_primary_net_pnl": candidate_total,
                "stable_region_candidate": stable,
                "status": "STABLE" if stable else "ISOLATED_OR_UNSTABLE",
            }
        )
    return output


def local_region(center: Combination) -> tuple[Combination, ...]:
    """Return the deterministic adjacent 2x2x2x2x2 factorial around a center."""
    axes: list[tuple[int, int]] = []
    for value, (_name, levels) in zip(center.values, FACTOR_LEVELS):
        if value == levels[0]:
            axes.append((value, value + 1))
        else:
            axes.append((value - 1, value))
    return tuple(Combination(*values) for values in product(*axes))


def select_local_regions(
    rows: Iterable[dict[str, Any]],
    *,
    maximum_regions: int = 2,
) -> list[dict[str, Any]]:
    rows = tuple(rows)
    summaries = phase1_profile_summaries(rows)
    primary = {
        (row["combination_id"], row["direction"]): row
        for row in summaries
        if row["scenario"] == "PRIMARY_ZERO_MAKER"
    }
    stress = {
        (row["combination_id"], row["direction"]): row
        for row in summaries
        if row["scenario"] == "EXECUTION_STRESS"
    }
    seed_totals: dict[tuple[str, str, str], float] = {}
    for row in rows:
        if str(row["scenario"]) != "PRIMARY_ZERO_MAKER":
            continue
        key = (
            str(row["combination_id"]),
            str(row["direction"]),
            str(row["seed"]),
        )
        seed_totals[key] = seed_totals.get(key, 0.0) + float(row["net_pnl"])
    positive_seeds: dict[tuple[str, str], int] = {}
    for (combination_id, direction, _seed), total in seed_totals.items():
        if total > 0:
            key = (combination_id, direction)
            positive_seeds[key] = positive_seeds.get(key, 0) + 1
    candidates = [
        row
        for row in combination_neighborhood_rows(rows)
        if primary.get((row["combination_id"], row["direction"]), {}).get("window_count", 0) >= 4
        and primary.get((row["combination_id"], row["direction"]), {}).get("total_pnl", 0) > 0
        and primary.get((row["combination_id"], row["direction"]), {}).get("median_window_pnl", -1) > 0
        and primary.get((row["combination_id"], row["direction"]), {}).get("profit_factor", 0) >= 1.05
        and primary.get((row["combination_id"], row["direction"]), {}).get("positive_window_ratio", 0) >= 0.50
        and primary.get((row["combination_id"], row["direction"]), {}).get("inventory_drag_ratio", float("inf")) <= 0.75
        and stress.get((row["combination_id"], row["direction"]), {}).get("total_pnl", -1) >= 0
        and positive_seeds.get((row["combination_id"], row["direction"]), 0) >= 4
    ]
    candidates.sort(
        key=lambda row: (
            bool(row["stable_region_candidate"]),
            primary[(row["combination_id"], row["direction"])]["median_window_pnl"],
            stress[(row["combination_id"], row["direction"])]["total_pnl"],
            -primary[(row["combination_id"], row["direction"])]["inventory_drag_ratio"],
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        center = Combination.parse(candidate["combination_id"])
        members = local_region(center)
        selected.append(
            {
                "region_id": f"REGION_{len(selected) + 1}",
                "center_combination_id": center.id,
                "direction": candidate["direction"],
                "member_ids": ";".join(member.id for member in members),
                "member_count": len(members),
                "selection_status": (
                    "NEIGHBOR_STABLE"
                    if candidate["stable_region_candidate"]
                    else "PROVISIONAL_LOCAL_VALIDATION"
                ),
            }
        )
        if len(selected) >= maximum_regions:
            break
    return selected
