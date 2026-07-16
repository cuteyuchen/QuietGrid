from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from random import Random
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class WalkForwardConfig:
    train_rows: int
    test_rows: int
    step_rows: int | None = None
    anchored: bool = False


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass(frozen=True)
class MonteCarloConfig:
    simulations: int = 1000
    seed: int = 17
    missing_positive_fill_probability: float = 0.05
    loss_multiplier: float = 1.15
    cost_per_event: float = 0.0


def build_walk_forward_folds(
    total_rows: int,
    config: WalkForwardConfig,
) -> list[WalkForwardFold]:
    if total_rows < 1:
        return []
    if config.train_rows < 2:
        raise ValueError("walk-forward train_rows至少为2。")
    if config.test_rows < 1:
        raise ValueError("walk-forward test_rows至少为1。")
    step_rows = config.step_rows or config.test_rows
    if step_rows < 1:
        raise ValueError("walk-forward step_rows至少为1。")

    folds: list[WalkForwardFold] = []
    fold_number = 1
    train_start = 0
    train_end = config.train_rows
    while train_end + config.test_rows <= total_rows:
        folds.append(
            WalkForwardFold(
                fold=fold_number,
                train_start=0 if config.anchored else train_start,
                train_end=train_end,
                test_start=train_end,
                test_end=train_end + config.test_rows,
            )
        )
        fold_number += 1
        train_start += step_rows
        train_end += step_rows
    return folds


def evaluate_walk_forward(
    rows: Sequence[Any],
    config: WalkForwardConfig,
    evaluator: Callable[[Sequence[Any], Sequence[Any], WalkForwardFold], dict[str, Any]],
) -> dict[str, Any]:
    folds = build_walk_forward_folds(len(rows), config)
    results: list[dict[str, Any]] = []
    for fold in folds:
        train = rows[fold.train_start : fold.train_end]
        test = rows[fold.test_start : fold.test_end]
        if fold.train_end > fold.test_start:
            raise RuntimeError("walk-forward 训练集与测试集重叠。")
        metrics = evaluator(train, test, fold)
        results.append(
            {
                "fold": fold.fold,
                "train_start": fold.train_start,
                "train_end": fold.train_end,
                "test_start": fold.test_start,
                "test_end": fold.test_end,
                **metrics,
            }
        )
    pnl_values = [
        float(item["total_pnl"])
        for item in results
        if _finite_number(item.get("total_pnl")) is not None
    ]
    drawdowns = [
        float(item["max_drawdown"])
        for item in results
        if _finite_number(item.get("max_drawdown")) is not None
    ]
    return {
        "status": "COMPLETED" if results else "INSUFFICIENT_DATA",
        "fold_count": len(results),
        "profitable_fold_ratio": (
            sum(1 for value in pnl_values if value > 0) / len(pnl_values)
            if pnl_values
            else 0.0
        ),
        "aggregate_pnl": sum(pnl_values),
        "average_pnl": (
            sum(pnl_values) / len(pnl_values)
            if pnl_values
            else 0.0
        ),
        "worst_fold_pnl": min(pnl_values) if pnl_values else 0.0,
        "worst_fold_drawdown": max(drawdowns) if drawdowns else 0.0,
        "folds": results,
    }


def monte_carlo_resample(
    event_returns: Sequence[float],
    config: MonteCarloConfig | None = None,
) -> dict[str, Any]:
    config = config or MonteCarloConfig()
    _validate_monte_carlo(config)
    source = [_required_finite(value, "event_return") for value in event_returns]
    if not source:
        return {
            "status": "INSUFFICIENT_DATA",
            "simulations": 0,
            "total_pnl_p05": 0.0,
            "total_pnl_p50": 0.0,
            "total_pnl_p95": 0.0,
            "max_drawdown_p95": 0.0,
            "max_drawdown_p99": 0.0,
            "loss_probability": 0.0,
        }

    random = Random(config.seed)
    totals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(config.simulations):
        sample = list(source)
        random.shuffle(sample)
        stressed: list[float] = []
        for value in sample:
            if value > 0 and random.random() < config.missing_positive_fill_probability:
                adjusted = 0.0
            elif value < 0:
                adjusted = value * config.loss_multiplier
            else:
                adjusted = value
            stressed.append(adjusted - config.cost_per_event)
        totals.append(sum(stressed))
        drawdowns.append(_max_drawdown(stressed))

    return {
        "status": "COMPLETED",
        "simulations": config.simulations,
        "seed": config.seed,
        "total_pnl_p05": _quantile(totals, 0.05),
        "total_pnl_p50": _quantile(totals, 0.50),
        "total_pnl_p95": _quantile(totals, 0.95),
        "max_drawdown_p95": _quantile(drawdowns, 0.95),
        "max_drawdown_p99": _quantile(drawdowns, 0.99),
        "loss_probability": sum(1 for value in totals if value < 0) / len(totals),
    }


def _max_drawdown(changes: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for change in changes:
        equity += change
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _validate_monte_carlo(config: MonteCarloConfig) -> None:
    if not 1 <= config.simulations <= 100_000:
        raise ValueError("Monte Carlo simulations必须在1到100000之间。")
    if not 0 <= config.missing_positive_fill_probability <= 1:
        raise ValueError("missing_positive_fill_probability必须在0到1之间。")
    if config.loss_multiplier < 1:
        raise ValueError("loss_multiplier不能小于1。")
    _required_finite(config.cost_per_event, "cost_per_event")


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _required_finite(value: Any, label: str) -> float:
    result = _finite_number(value)
    if result is None:
        raise ValueError(f"{label}必须为有限数。")
    return result
