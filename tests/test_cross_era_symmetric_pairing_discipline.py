from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_symmetric_pairing_discipline as pairing
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_round13_diagnose import _sha256


def _changed_fields(left: object, right: object) -> set[str]:
    left_values = asdict(left)  # type: ignore[arg-type]
    right_values = asdict(right)  # type: ignore[arg-type]
    return {
        key
        for key in left_values
        if left_values[key] != right_values[key]
    }


def _cache(
    policies: dict,
    *,
    window_ids: tuple[str, ...] = ("w1", "w2"),
    cost: tuple[float, float, float] = asset_audit.SCENARIOS["BASE"],
    seed: int = 3,
) -> dict[tuple[object, ...], object]:
    maker_fee, taker_fee, slippage_bps = cost
    return {
        (
            policy.parameter.parameter_id,
            symbol,
            window_id,
            0.65,
            2160,
            policy.max_inventory_notional,
            5,
            1.10,
            1.0,
            1,
            0.75,
            maker_fee,
            taker_fee,
            slippage_bps,
            2.0,
            seed,
        ): object()
        for window_id in window_ids
        for symbol, policy in policies.items()
    }


def test_protocol_hash_matches_preregistered_file() -> None:
    assert _sha256(pairing.PROTOCOL_PATH.resolve()) == pairing.PROTOCOL_SHA256


def test_candidate_changes_only_preregistered_pairing_fields() -> None:
    base_config = profit_opt._base_research_config()
    locked_parameters, locked_policies, locked_maker = profit_opt._locked_policy()

    parameters, policies, config, maker_policy = pairing._candidate_components(
        base_config
    )

    assert parameters == locked_parameters
    assert _changed_fields(locked_policies["BTCUSDT"], policies["BTCUSDT"]) == {
        "reduce_target_step_fraction"
    }
    assert _changed_fields(locked_policies["ETHUSDT"], policies["ETHUSDT"]) == {
        "entry_filter",
        "max_unpaired_lots_per_side",
        "reduce_target_step_fraction",
    }
    assert policies["BTCUSDT"].max_unpaired_lots_per_side == 1
    assert policies["ETHUSDT"].max_unpaired_lots_per_side == 1
    assert policies["BTCUSDT"].reduce_target_step_fraction == pytest.approx(0.75)
    assert policies["ETHUSDT"].reduce_target_step_fraction == pytest.approx(0.75)
    assert all(policy.entry_filter is None for policy in policies.values())
    assert _changed_fields(base_config, config) == {"wind_down_bars"}
    assert config.wind_down_bars == 2160
    assert _changed_fields(locked_maker, maker_policy) == {"urgency_exponent"}
    assert maker_policy.urgency_exponent == pytest.approx(2.0)


def test_candidate_payload_is_json_serializable_and_neutral() -> None:
    payload = pairing._candidate_payload(profit_opt._base_research_config())

    assert payload["candidate_id"] == pairing.CANDIDATE_ID
    assert payload["direction_mode"] == "NEUTRAL"
    assert {
        item["parameter"]["direction_mode"]
        for item in payload["symbol_policies"].values()
    } == {"NEUTRAL"}
    assert {
        item["max_unpaired_lots_per_side"]
        for item in payload["symbol_policies"].values()
    } == {1}
    assert {
        item["reduce_target_step_fraction"]
        for item in payload["symbol_policies"].values()
    } == {0.75}
    json.dumps(payload)


def test_worker_cache_accepts_exact_candidate_fingerprint() -> None:
    _parameters, policies, config, maker_policy = pairing._candidate_components(
        profit_opt._base_research_config()
    )
    research = SimpleNamespace(config=config, _cache=_cache(policies))

    observed = pairing._verify_worker_cache(
        research,
        policies,
        maker_policy,
        allowed_window_ids={"w1", "w2"},
        cost=asset_audit.SCENARIOS["BASE"],
        seed=3,
    )

    assert observed["window_count"] == 2
    assert observed["symbol_window_count"] == 4
    assert observed["max_unpaired_lots_per_side"] == 1
    assert observed["reduce_target_step_fraction"] == pytest.approx(0.75)
    assert observed["entry_filters_enabled"] is False
    assert observed["passed"] is True


def test_worker_cache_rejects_target_drift() -> None:
    _parameters, policies, config, maker_policy = pairing._candidate_components(
        profit_opt._base_research_config()
    )
    cache = _cache(policies)
    key = next(iter(cache))
    del cache[key]
    changed = list(key)
    changed[10] = 0.50
    cache[tuple(changed)] = object()
    research = SimpleNamespace(config=config, _cache=cache)

    with pytest.raises(RuntimeError, match="参数 10 不一致"):
        pairing._verify_worker_cache(
            research,
            policies,
            maker_policy,
            allowed_window_ids={"w1", "w2"},
            cost=asset_audit.SCENARIOS["BASE"],
            seed=3,
        )


def test_worker_cache_rejects_unauthorized_window() -> None:
    _parameters, policies, config, maker_policy = pairing._candidate_components(
        profit_opt._base_research_config()
    )
    research = SimpleNamespace(config=config, _cache=_cache(policies))

    with pytest.raises(RuntimeError, match="授权窗口之外"):
        pairing._verify_worker_cache(
            research,
            policies,
            maker_policy,
            allowed_window_ids={"w1"},
            cost=asset_audit.SCENARIOS["BASE"],
            seed=3,
        )
