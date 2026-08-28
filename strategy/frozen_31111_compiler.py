from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from strategy.frozen_31111_runtime import Frozen31111Runtime


COMPILER_VERSION = "frozen-31111-controller-compiler-v1"
BASE_COMMIT = "1e1d45f816beb92d51df6bbcbbdc753eb12105fc"


class FrozenRuntimeParityError(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise FrozenRuntimeParityError(f"frozen artifact missing {key}")
    return mapping[key]


def _head_commit(repo_root: str | Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        return BASE_COMMIT


@dataclass(frozen=True)
class Frozen31111ControllerCompiler:
    """Compile only the attested v2.9 candidate into TradingController inputs."""

    frozen: Frozen31111Runtime
    repo_root: str | Path = "."
    database_path: str | Path = "data/runtime/v41/controller.sqlite"

    @classmethod
    def load(
        cls,
        repo_root: str | Path = ".",
        database_path: str | Path = "data/runtime/v41/controller.sqlite",
    ) -> "Frozen31111ControllerCompiler":
        return cls(Frozen31111Runtime.load(repo_root), repo_root, database_path)

    def compile(self) -> dict[str, Any]:
        candidate = self.frozen.frozen.candidate
        if self.frozen.candidate_id != "31111-NEUTRAL":
            raise FrozenRuntimeParityError("candidate id mismatch")
        if self.frozen.candidate_sha != "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774":
            raise FrozenRuntimeParityError("candidate SHA mismatch")
        combination = deepcopy(_required(candidate, "combination_definition"))
        parameters = deepcopy(_required(candidate, "parameters"))
        raw = deepcopy(_required(self.frozen.frozen.config, "frozen_sections"))
        range_parameters = _required(parameters, "range_and_volatility")
        grid_parameters = _required(parameters, "grid")
        profit_parameters = _required(parameters, "profit_protection")
        inventory_parameters = _required(parameters, "inventory")
        stop_parameters = _required(parameters, "stop_and_window_loss")
        capital = _required(parameters, "capital_and_leverage")

        if combination.get("combination_id") != "31111":
            raise FrozenRuntimeParityError("combination id mismatch")
        if str(candidate.get("direction", "")).upper() != "NEUTRAL":
            raise FrozenRuntimeParityError("candidate direction is not NEUTRAL")
        if int(capital.get("economic_leverage", 0)) != 1:
            raise FrozenRuntimeParityError("candidate economic leverage is not 1x")

        range_data = _required(combination, "range")
        grid_data = _required(combination, "grid")
        profit_data = _required(combination, "profit")
        inventory_data = _required(combination, "inventory")
        stop_data = _required(combination, "stop")
        multiplier = float(range_data["multiplier"])
        if int(range_data["level"]) != 3 or not range_data["adaptive"] is False or multiplier != 2.0:
            raise FrozenRuntimeParityError("A3 range semantics mismatch")
        if int(grid_data["level"]) != 1 or int(grid_data["min_grid_num"]) != 5 or int(grid_data["max_grid_num"]) != 10 or float(grid_data["step_multiplier"]) != 1.5:
            raise FrozenRuntimeParityError("B1 grid semantics mismatch")
        if int(profit_data["level"]) != 1 or str(profit_data["mode"]).upper() != "NONE":
            raise FrozenRuntimeParityError("C1 profit semantics mismatch")
        if int(inventory_data["level"]) != 1 or float(inventory_data["caution"]) != 0.4 or float(inventory_data["reduce_only"]) != 0.8:
            raise FrozenRuntimeParityError("D1 inventory semantics mismatch")
        if int(stop_data["level"]) != 1 or str(stop_data["mode"]).upper() != "BASELINE" or float(stop_data["atr_buffer"]) != 0.0:
            raise FrozenRuntimeParityError("E1 stop semantics mismatch")

        research = deepcopy(raw.get("semiconductor_grid", {}))
        symbol_profiles = deepcopy(research.get("symbol_profiles", {}))
        live_symbols = tuple(str(symbol).upper() for symbol in research.get("live_symbols", []))
        normal_steps = {}
        capital_multipliers = {}
        for symbol in self.frozen.symbols:
            profile = _required(symbol_profiles, symbol)
            if symbol not in live_symbols:
                continue
            normal_steps[symbol] = float(profile["normal_min_step_pct"]) * float(grid_data["step_multiplier"])
            capital_multipliers[symbol] = float(profile["capital_multiplier"])

        grid = deepcopy(raw["grid"])
        grid["k_atr_range"] = float(range_parameters["base_k_atr_range"])
        grid["k_sigma_range"] = float(range_parameters["base_k_sigma_range"])
        grid["max_range_pct"] = float(range_parameters["base_max_range_pct"]) * multiplier
        grid["min_grid_num"] = int(grid_data["min_grid_num"])
        grid["max_grid_num"] = int(grid_data["max_grid_num"])
        grid["min_step_pct"] = min(normal_steps.values()) if normal_steps else float(grid.get("min_step_pct", 0.0))
        grid["min_step_pct_by_symbol"] = normal_steps
        grid["range_multiplier_by_symbol"] = {symbol: multiplier for symbol in live_symbols}
        grid["max_range_pct_by_symbol"] = {symbol: float(grid["max_range_pct"]) for symbol in live_symbols}

        raw["grid"] = grid
        raw["trading"]["direction_mode"] = str(candidate["direction"]).upper()
        raw["trading"]["leverage"] = int(capital["economic_leverage"])
        raw["trading"]["capital_per_symbol"] = float(capital["capital_per_symbol"])
        raw["trading"]["profit_protection_enabled"] = str(profit_data["mode"]).upper() != "NONE"
        raw["trading"]["take_profit_usdt"] = (
            float(profit_parameters["fixed_take_profit_usdt"])
            if str(profit_data["mode"]).upper() != "NONE"
            else 0.0
        )
        raw["trading"]["capital_multiplier_by_symbol"] = capital_multipliers

        production_inventory = deepcopy(_required(inventory_parameters, "production_inventory"))
        raw["inventory"]["caution_utilization"] = float(inventory_data["caution"])
        raw["inventory"]["critical_utilization"] = float(production_inventory["critical_utilization"])
        raw["inventory"]["suppress_same_side_orders"] = bool(production_inventory["suppress_same_side_orders"])
        raw["inventory"]["passive_reduce_first"] = bool(production_inventory["passive_reduce_first"])
        raw["inventory"]["max_unpaired_lots_per_side_by_symbol"] = deepcopy(
            production_inventory["max_unpaired_lots_per_side_by_symbol"]
        )

        selection = deepcopy(raw.get("selection", {}))
        selection.update(
            {
                "symbol_allowlist": list(live_symbols),
                "symbol_blacklist": [],
                "scan_candidate_count": len(live_symbols),
                "volume_weight": float(selection.get("volume_weight", 0.7)),
                "depth_weight": float(selection.get("depth_weight", 0.3)),
                "depth_levels": int(selection.get("depth_levels", 5)),
            }
        )
        raw["selection"] = selection
        raw["features"] = {
            "regime_v2": True,
            "inventory_manager": True,
            "adaptive_grid_v2": True,
            "risk_manager_v2": True,
        }
        raw["database"] = {"path": str(Path(self.database_path))}
        raw.setdefault("notifications", {})
        raw["proxy"] = {"enabled": False}

        semantics = {
            "candidate_id": self.frozen.candidate_id,
            "candidate_sha": self.frozen.candidate_sha,
            "direction": str(candidate["direction"]).upper(),
            "economic_leverage": int(capital["economic_leverage"]),
            "range": {
                **range_data,
                "base_k_atr_range": float(range_parameters["base_k_atr_range"]),
                "base_k_sigma_range": float(range_parameters["base_k_sigma_range"]),
                "base_max_range_pct": float(range_parameters["base_max_range_pct"]),
                "effective_k_atr_range": float(range_parameters["base_k_atr_range"]) * multiplier,
                "effective_k_sigma_range": float(range_parameters["base_k_sigma_range"]) * multiplier,
                "effective_max_range_pct": float(range_parameters["base_max_range_pct"]) * multiplier,
            },
            "grid": {
                **grid_data,
                "min_step_pct_by_symbol": normal_steps,
                "cost_floor_logic": deepcopy(grid_parameters["cost_floor_logic"]),
            },
            "profit": {
                **profit_data,
                "take_profit_usdt": 0.0,
                "profit_protection_enabled": False,
            },
            "inventory": {
                **inventory_data,
                "production_inventory": production_inventory,
            },
            "stop": {
                **stop_data,
                "baseline_stop_buffer_pct": float(raw["trading"]["stop_buffer_pct"]),
                "additional_stop_atr_buffer": 0.0,
            },
            "capital": {
                "capital_per_symbol": float(capital["capital_per_symbol"]),
                "capital_multiplier_by_symbol": capital_multipliers,
            },
            "symbol_profiles": {
                symbol: {
                    **_required(symbol_profiles, symbol),
                    "runtime_role": "SHADOW_ELIGIBLE" if symbol in live_symbols else "RESEARCH_ONLY",
                }
                for symbol in self.frozen.symbols
            },
            "live_symbols": list(live_symbols),
            "research_only_symbols": [symbol for symbol in self.frozen.symbols if symbol not in live_symbols],
            "exposure_cutoff": self.frozen.exposure_cutoff,
        }
        semantics["effective_strategy_sha"] = _sha256(
            {key: value for key, value in semantics.items() if key != "effective_strategy_sha"}
        )

        effective_raw_sha = _sha256(raw)
        manifest = {
            "schema_version": 1,
            "compiler_version": COMPILER_VERSION,
            "candidate_id": self.frozen.candidate_id,
            "candidate_sha": self.frozen.candidate_sha,
            "freeze_tag": "semiconductor-grid-forward-oos-v2.9-freeze",
            "freeze_artifact_blob_sha": self._freeze_blob_sha(),
            "source_commit": _head_commit(self.repo_root),
            "runtime_commit": _head_commit(self.repo_root),
            "effective_strategy_sha": semantics["effective_strategy_sha"],
            "effective_runtime_config_sha": effective_raw_sha,
            "strategy_semantics": semantics,
            "effective_raw_config": raw,
        }
        return manifest

    @property
    def raw_config(self) -> dict[str, Any]:
        return self.compile()["effective_raw_config"]

    def _freeze_blob_sha(self) -> str:
        try:
            return subprocess.check_output(
                [
                    "git", "-C", str(self.repo_root), "rev-parse",
                    "semiconductor-grid-forward-oos-v2.9-freeze:reports/semiconductor-grid-forward-oos-v2.9/candidate-freeze.json",
                ],
                text=True,
            ).strip()
        except Exception as exc:
            raise FrozenRuntimeParityError("freeze artifact is not anchored by the formal tag") from exc


def assert_frozen_runtime_parity(manifest: dict[str, Any], controller: Any | None = None) -> None:
    semantics = manifest.get("strategy_semantics")
    expected_sha = "776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3"
    if not isinstance(semantics, dict):
        raise FrozenRuntimeParityError("effective strategy semantics missing")
    if semantics.get("effective_strategy_sha") != expected_sha:
        raise FrozenRuntimeParityError("FROZEN_RUNTIME_PARITY_MISMATCH: effective strategy SHA")
    if semantics.get("candidate_sha") != "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774":
        raise FrozenRuntimeParityError("FROZEN_RUNTIME_PARITY_MISMATCH: candidate SHA")
    if controller is not None:
        _assert_controller_parity(manifest, controller)


def _assert_controller_parity(manifest: dict[str, Any], controller: Any) -> None:
    from strategy.controller import TradingController

    if not isinstance(controller, TradingController):
        return
    semantics = manifest["strategy_semantics"]
    config = controller.config
    if getattr(controller, "frozen_candidate_sha", "") != semantics["candidate_sha"]:
        raise FrozenRuntimeParityError("FROZEN_RUNTIME_PARITY_MISMATCH: controller metadata")
    if config.direction_mode.value != semantics["direction"] or int(config.leverage) != 1:
        raise FrozenRuntimeParityError("FROZEN_RUNTIME_PARITY_MISMATCH: direction or leverage")
    if config.take_profit_usdt != 0.0 or controller.risk.config.profit_protection_enabled:
        raise FrozenRuntimeParityError("FROZEN_RUNTIME_PARITY_MISMATCH: C1 is active")
    grid = semantics["grid"]
    for symbol, minimum_step in grid["min_step_pct_by_symbol"].items():
        generator = controller._adaptive_grid_for_symbol(symbol)
        expected_multiplier = semantics["range"]["multiplier"]
        if (
            generator.config.min_grid_num != grid["min_grid_num"]
            or generator.config.max_grid_num != grid["max_grid_num"]
            or generator.config.min_step_pct != minimum_step
            or generator.config.k_atr_range != semantics["range"]["base_k_atr_range"] * expected_multiplier
            or generator.config.k_sigma_range != semantics["range"]["base_k_sigma_range"] * expected_multiplier
            or generator.config.max_range_pct != semantics["range"]["effective_max_range_pct"]
        ):
            raise FrozenRuntimeParityError(f"FROZEN_RUNTIME_PARITY_MISMATCH: grid for {symbol}")
    if controller.inventory.config.caution_utilization != semantics["inventory"]["caution"]:
        raise FrozenRuntimeParityError("FROZEN_RUNTIME_PARITY_MISMATCH: inventory caution")
    if controller.inventory.config.critical_utilization != semantics["inventory"]["reduce_only"]:
        raise FrozenRuntimeParityError("FROZEN_RUNTIME_PARITY_MISMATCH: inventory reduce-only threshold")
    for symbol, multiplier in semantics["capital"]["capital_multiplier_by_symbol"].items():
        if controller._capital_for_symbol(symbol) != semantics["capital"]["capital_per_symbol"] * multiplier:
            raise FrozenRuntimeParityError(f"FROZEN_RUNTIME_PARITY_MISMATCH: capital for {symbol}")
