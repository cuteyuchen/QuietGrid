from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.cross_era_pre2020_quadratic_w2160 as round13
from scripts.cross_era_asset_scope_audit import (
    EXPECTED_CURRENT_SPLIT_COUNTS,
    _scope_checks,
    _summarize_symbol,
    _validate_external_execution_integrity,
    _validate_frozen_dataset,
    _validated_current_splits,
)
from scripts.robustness import AggregateMetrics


def _metrics(
    pnl: float,
    *,
    profit_factor: float | None = 1.2,
    drawdown: float = 0.02,
    concentration: float = 0.20,
    coverage: float = 0.50,
) -> AggregateMetrics:
    return AggregateMetrics(
        window_count=10,
        symbol_window_count=10,
        traded_symbol_windows=5,
        blocked_symbol_windows=5,
        total_pnl=pnl,
        total_return_pct=pnl / 300.0,
        annualized_return_pct=0.1,
        max_drawdown=drawdown * 300.0,
        max_drawdown_pct=drawdown,
        positive_window_ratio=0.5,
        profit_factor=profit_factor,
        sharpe_per_week=1.0,
        trade_coverage=coverage,
        pair_count=10,
        fill_count=20,
        fees_paid=1.0,
        funding_paid=0.0,
        exit_slippage_cost=0.2,
        best_window_concentration=concentration,
        objective=0.1,
    )


def test_summarize_symbol_requires_all_six_frozen_seeds() -> None:
    metrics = {
        seed: _metrics(float(index + 1))
        for index, seed in enumerate((3, 10, 17, 31, 59, 97))
    }

    summary = _summarize_symbol(metrics)

    assert summary["positive_seed_count"] == 6
    assert summary["worst_seed_total_pnl"] == 1.0
    assert summary["all_seed_profit_factors_gt_1"] is True


def test_scope_checks_rejects_concentration_and_negative_seed() -> None:
    metrics = {
        seed: _metrics(
            -1.0 if seed == 97 else 2.0,
            concentration=0.40 if seed == 59 else 0.20,
        )
        for seed in (3, 10, 17, 31, 59, 97)
    }
    summary = _summarize_symbol(metrics)

    checks = _scope_checks(summary)

    assert checks["all_seeds_positive"] is False
    assert checks["worst_seed_positive"] is False
    assert checks["best_window_concentration_le_35pct"] is False


def test_profit_factor_none_passes_only_for_positive_no_loss_run() -> None:
    positive = {
        seed: _metrics(2.0, profit_factor=None)
        for seed in (3, 10, 17, 31, 59, 97)
    }
    negative = dict(positive)
    negative[97] = _metrics(-1.0, profit_factor=None)

    assert _summarize_symbol(positive)["all_seed_profit_factors_gt_1"] is True
    assert _summarize_symbol(negative)["all_seed_profit_factors_gt_1"] is False


def test_validate_frozen_dataset_requires_exact_snapshot() -> None:
    expected = [{"dataset_id": "btc", "row_count": 10}]

    _validate_frozen_dataset(expected, expected, label="CURRENT")

    with pytest.raises(ValueError, match="CURRENT 冻结数据"):
        _validate_frozen_dataset(
            [{"dataset_id": "btc", "row_count": 9}],
            expected,
            label="CURRENT",
        )


def test_validated_current_splits_enforces_counts_and_isolation() -> None:
    development = tuple(f"d-{index}" for index in range(108))
    validation = tuple(f"v-{index}" for index in range(54))
    final_oos = tuple(f"o-{index}" for index in range(54))
    recorded = {
        "development_count": 108,
        "validation_count": 54,
        "final_oos_count": 54,
    }

    accessed = _validated_current_splits(
        SimpleNamespace(
            development=development,
            validation=validation,
            final_oos=final_oos,
        ),
        recorded,
    )

    assert {name: len(ids) for name, ids in accessed.items()} == {
        "development": EXPECTED_CURRENT_SPLIT_COUNTS["development"],
        "validation": EXPECTED_CURRENT_SPLIT_COUNTS["validation"],
    }
    assert not (set(accessed["development"]) & set(final_oos))
    assert not (set(accessed["validation"]) & set(final_oos))

    with pytest.raises(RuntimeError, match="切分重叠"):
        _validated_current_splits(
            SimpleNamespace(
                development=development,
                validation=validation,
                final_oos=(development[0], *final_oos[1:]),
            ),
            recorded,
        )


def test_external_execution_integrity_must_match_round13_candidate() -> None:
    expected = {
        "window_count": 28,
        "symbol_window_count": 56,
        "wind_down_bars": 2160,
        "urgency_exponent": 2.0,
        "passed": True,
    }
    recorded = {round13.CANDIDATE_ID: expected}

    _validate_external_execution_integrity(expected, recorded)

    with pytest.raises(RuntimeError, match="执行完整性"):
        _validate_external_execution_integrity(
            {**expected, "wind_down_bars": 1440},
            recorded,
        )
