from __future__ import annotations

from types import SimpleNamespace

import scripts.cross_era_loss_conditioned_wind_down as round7
from scripts.profit_protection_optimize import BASE_COST
from scripts.robustness import ResearchConfig


def test_round7_candidates_and_filters_match_protocol() -> None:
    assert round7._registered_thresholds() == (0.20, 0.40)
    assert round7._candidate_id(0.20) == "IL20"
    assert round7._candidate_id(0.40) == "IL40"
    assert round7.FIXED_FILTERS["BTCUSDT"].filter_id == "de0.40_ve1.05_rr0.35"
    assert round7.FIXED_FILTERS["ETHUSDT"].filter_id == "de0.35_ve1.05_rr0.55"


def test_round7_cells_exclude_final_oos() -> None:
    split = SimpleNamespace(
        development=("dev",),
        validation=("val",),
        final_oos=("final",),
    )

    cells = round7._cell_specs(split)

    assert [item[0] for item in cells] == [
        "DEV_BASE",
        "DEV_COST50",
        "VAL_BASE",
        "VAL_COST50",
    ]
    assert {window_id for _, _, window_ids, _ in cells for window_id in window_ids} == {
        "dev",
        "val",
    }


def test_round7_worker_applies_loss_condition_without_changing_fallback(
    monkeypatch,
) -> None:
    captured = []

    class FakeResearch:
        def __init__(self, windows, parameters, config, dataset_metadata):
            captured.append(config)

        def evaluate_joint_policy_windows(self, *args, **kwargs):
            return SimpleNamespace(), []

    monkeypatch.setattr(round7, "RobustnessResearch", FakeResearch)
    monkeypatch.setattr(
        round7.profit_opt,
        "_WORKER_STATE",
        {
            "base_config": ResearchConfig(wind_down_bars=1440),
            "windows": [],
            "parameters": [],
            "metadata": [],
            "symbol_policies": {},
            "maker_policy": SimpleNamespace(),
        },
    )

    threshold, seed, runs = round7._loss_conditioned_wind_down_seed_worker(
        0.20,
        3,
        {"development": ("dev",), "validation": ("val",)},
        BASE_COST,
    )

    assert threshold == 0.20
    assert seed == 3
    assert set(runs) == {"development", "validation"}
    assert captured[0].wind_down_bars == 1440
    assert captured[0].inventory_wind_down_bars == 2880
    assert captured[0].inventory_wind_down_utilization == 0.20
    assert captured[0].inventory_wind_down_only_when_losing is True


def test_round7_reference_disables_inventory_condition(monkeypatch) -> None:
    captured = []

    class FakeResearch:
        def __init__(self, windows, parameters, config, dataset_metadata):
            captured.append(config)

        def evaluate_joint_policy_windows(self, *args, **kwargs):
            return SimpleNamespace(), []

    monkeypatch.setattr(round7, "RobustnessResearch", FakeResearch)
    monkeypatch.setattr(
        round7.profit_opt,
        "_WORKER_STATE",
        {
            "base_config": ResearchConfig(wind_down_bars=1440),
            "windows": [],
            "parameters": [],
            "metadata": [],
            "symbol_policies": {},
            "maker_policy": SimpleNamespace(),
        },
    )

    round7._loss_conditioned_wind_down_seed_worker(
        0.0,
        3,
        {"development": ("dev",)},
        BASE_COST,
    )

    assert captured[0].wind_down_bars == 1440
    assert captured[0].inventory_wind_down_bars == 0
    assert captured[0].inventory_wind_down_utilization == 0.0
    assert captured[0].inventory_wind_down_only_when_losing is False
