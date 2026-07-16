from __future__ import annotations

from strategy.grid_calculator import GridCalculationError, GridConfig, calculate_grid_params


def _klines(count: int = 60) -> list[dict[str, float]]:
    rows = []
    for index in range(count):
        close = 100 + ((index % 10) - 5) * 0.05
        rows.append({"open": close, "high": close + 0.08, "low": close - 0.08, "close": close})
    return rows


def test_calculate_grid_params_from_std_range() -> None:
    params = calculate_grid_params(
        symbol="AAPLUSDT",
        klines=_klines(),
        current_price=100.0,
        funding_rate=0.0001,
        config=GridConfig(),
    )

    assert params.symbol == "AAPLUSDT"
    assert params.lower < 100 < params.upper
    assert 1 <= params.grid_num <= 20
    assert len(params.grid_prices) == params.grid_num + 1
    assert params.step_pct >= 0.0015
    assert params.stop_loss_price < params.lower
    assert params.baseline_atr > 0


def test_calculate_grid_params_from_ohlc_volatility_methods() -> None:
    for method in ("parkinson", "garman_klass", "rogers_satchell", "yang_zhang"):
        params = calculate_grid_params(
            symbol="AAPLUSDT",
            klines=_klines(),
            current_price=100.0,
            funding_rate=0.0001,
            config=GridConfig(range_method=method),
        )

        assert params.volatility_method == method
        assert params.volatility_value > 0
        assert params.volatility_window == 60
        assert params.lower < 100 < params.upper
        assert params.step_pct >= 0.0015


def test_rejects_not_enough_samples() -> None:
    try:
        calculate_grid_params("AAPLUSDT", _klines(10), 100.0, 0.0001, GridConfig())
    except GridCalculationError as exc:
        assert "样本不足" in str(exc)
    else:
        raise AssertionError("not enough samples should be rejected")


def test_rejects_current_price_outside_range() -> None:
    try:
        calculate_grid_params("AAPLUSDT", _klines(), 110.0, 0.0001, GridConfig())
    except GridCalculationError as exc:
        assert "漂移" in str(exc)
    else:
        raise AssertionError("outside price should be rejected")


def test_ohlc_volatility_range_rejects_current_price_outside_range() -> None:
    try:
        calculate_grid_params("AAPLUSDT", _klines(), 110.0, 0.0001, GridConfig(range_method="garman_klass"))
    except GridCalculationError as exc:
        assert "漂移" in str(exc)
    else:
        raise AssertionError("outside price should be rejected")


def test_ohlc_volatility_range_respects_max_range_pct() -> None:
    try:
        calculate_grid_params(
            "AAPLUSDT",
            _klines(),
            100.0,
            0.0001,
            GridConfig(range_method="parkinson", std_k=50, max_range_pct=0.01),
        )
    except GridCalculationError as exc:
        assert "区间宽度超过" in str(exc)
    else:
        raise AssertionError("range above max_range_pct should be rejected")


def test_grid_range_must_reach_minimum_tradable_threshold() -> None:
    try:
        calculate_grid_params(
            "AAPLUSDT",
            _klines(),
            100.0,
            0.0001,
            GridConfig(min_tradable_range_pct=0.02),
        )
    except GridCalculationError as exc:
        assert "最小可交易波动阈值" in str(exc)
    else:
        raise AssertionError("range below min_tradable_range_pct should be rejected")


def test_rejects_non_finite_or_non_positive_numeric_inputs() -> None:
    cases = [
        (_klines(), "nan", 0.0001, "当前价格"),
        (_klines(), 100.0, "inf", "资金费率"),
        ([{**row, "close": "nan"} if index == 0 else row for index, row in enumerate(_klines())], 100.0, 0.0001, "close"),
        ([{**row, "low": 0.0} if index == 0 else row for index, row in enumerate(_klines())], 100.0, 0.0001, "low"),
    ]

    for klines, current_price, funding_rate, expected in cases:
        try:
            calculate_grid_params("AAPLUSDT", klines, current_price, funding_rate, GridConfig())
        except GridCalculationError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("invalid numeric input should be rejected")


def test_rejects_inconsistent_kline_prices() -> None:
    cases = [
        [{**row, "high": row["low"] - 0.01} if index == 0 else row for index, row in enumerate(_klines())],
        [{**row, "close": row["high"] + 0.01} if index == 0 else row for index, row in enumerate(_klines())],
        [{**row, "close": row["low"] - 0.01} if index == 0 else row for index, row in enumerate(_klines())],
    ]

    for klines in cases:
        try:
            calculate_grid_params("AAPLUSDT", klines, 100.0, 0.0001, GridConfig())
        except GridCalculationError as exc:
            assert "K线价格关系非法" in str(exc)
        else:
            raise AssertionError("inconsistent kline prices should be rejected")


def test_rejects_invalid_grid_config_values() -> None:
    invalid_configs = [
        (GridConfig(std_k=0), "std_k"),
        (GridConfig(std_k=float("nan")), "std_k"),
        (GridConfig(quantile_lower=0.7, quantile_upper=0.3), "分位数"),
        (GridConfig(min_step_pct=0), "min_step_pct"),
        (GridConfig(min_tradable_range_pct=0), "min_tradable_range_pct"),
        (GridConfig(safety_multiplier=-1), "safety_multiplier"),
        (GridConfig(max_grid_num=0), "max_grid_num"),
        (GridConfig(max_range_pct=0), "max_range_pct"),
        (GridConfig(atr_period=0), "atr_period"),
        (GridConfig(stop_buffer_pct=-0.01), "stop_buffer_pct"),
        (GridConfig(stop_buffer_pct=1.0), "stop_buffer_pct"),
        (GridConfig(min_samples=0), "min_samples"),
        (GridConfig(rolling_regrid_seconds=0), "rolling_regrid_seconds"),
    ]

    for config, expected in invalid_configs:
        try:
            calculate_grid_params("AAPLUSDT", _klines(), 100.0, 0.0001, config)
        except GridCalculationError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("invalid grid config should be rejected")
