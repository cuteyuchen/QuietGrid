from __future__ import annotations

from strategy.validation import (
    MonteCarloConfig,
    WalkForwardConfig,
    build_walk_forward_folds,
    evaluate_walk_forward,
    monte_carlo_resample,
)


def test_walk_forward_folds_never_use_future_test_rows() -> None:
    folds = build_walk_forward_folds(
        30,
        WalkForwardConfig(train_rows=12, test_rows=6, step_rows=6),
    )

    assert [(fold.train_start, fold.train_end, fold.test_start, fold.test_end) for fold in folds] == [
        (0, 12, 12, 18),
        (6, 18, 18, 24),
        (12, 24, 24, 30),
    ]
    assert all(fold.train_end == fold.test_start for fold in folds)


def test_walk_forward_evaluation_reports_fold_distribution() -> None:
    rows = list(range(30))

    report = evaluate_walk_forward(
        rows,
        WalkForwardConfig(train_rows=12, test_rows=6, step_rows=6),
        lambda train, test, fold: {
            "total_pnl": float(sum(test) - sum(train[-2:])),
            "max_drawdown": float(fold.fold),
        },
    )

    assert report["status"] == "COMPLETED"
    assert report["fold_count"] == 3
    assert report["profitable_fold_ratio"] == 1.0
    assert report["worst_fold_drawdown"] == 3.0


def test_monte_carlo_is_deterministic_and_stresses_positive_fill_loss() -> None:
    config = MonteCarloConfig(
        simulations=200,
        seed=29,
        missing_positive_fill_probability=0.25,
        loss_multiplier=1.5,
        cost_per_event=0.01,
    )

    first = monte_carlo_resample([1.0, 0.8, -0.5, -0.2], config)
    second = monte_carlo_resample([1.0, 0.8, -0.5, -0.2], config)

    assert first == second
    assert first["simulations"] == 200
    assert first["total_pnl_p05"] <= first["total_pnl_p50"] <= first["total_pnl_p95"]
    assert first["max_drawdown_p99"] >= first["max_drawdown_p95"]


def test_monte_carlo_empty_input_is_explicit() -> None:
    report = monte_carlo_resample([])

    assert report["status"] == "INSUFFICIENT_DATA"
    assert report["simulations"] == 0
