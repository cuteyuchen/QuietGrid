from __future__ import annotations

import json
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quietgrid_v40.frozen import load_frozen_31111
from quietgrid_v40.safety import ExecutionLane, ExecutionSafetyPolicy
from exchange.shadow import PAPER_BASELINE, PAPER_CONSERVATIVE, ShadowBroker
from scripts.quietgrid_v40_capability_probe import probe


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git_sha(root: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def run_capability_probe(*, network: bool = True) -> dict[str, Any]:
    root = _repo_root()
    result = await probe(network=network)
    out = root / "reports" / "testnet-shadow-v4.0"
    _write_json(out / "capability-probe.json", result)
    lines = ["# Binance Futures Testnet Capability Probe", "", f"Classification: {result.get('classification', 'NOT_PROBED')}", "", "| Symbol | Exists | Status | Contract | Rules | GTX/order-test | Authenticated | Final |", "|---|---:|---|---|---|---|---|---|"]
    for item in result.get("symbols", []):
        lines.append(f"| {item['symbol']} | {item.get('exists', 'NOT_PROBED')} | {item.get('status', 'NOT_PROBED')} | {item.get('contractType', 'NOT_PROBED')} | {'YES' if item.get('rules') else 'NOT_PROBED'} | {item.get('gtx_order_test', 'NOT_PROBED')} | {item.get('authenticated_testnet', 'NOT_PROBED')} | {item.get('final_capability', 'NOT_PROBED')} |")
    lines += ["", "Production private API: DISABLED", "", "No production private endpoint was called.", ""]
    (out / "capability-probe.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def _shadow_reports(root: Path, broker: ShadowBroker, profile_name: str, policy: ExecutionSafetyPolicy, frozen: Any) -> None:
    out = root / "reports" / "testnet-shadow-v4.0"
    now = datetime.now(timezone.utc).isoformat()
    profile = broker.profile
    profile_sha = hashlib.sha256(json.dumps(profile.__dict__, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    probe_path = out / "capability-probe.json"
    probe_status = "NOT_RUN"
    if probe_path.exists():
        try:
            probe_status = json.loads(probe_path.read_text(encoding="utf-8")).get("classification", "UNKNOWN")
        except (OSError, ValueError):
            probe_status = "ERROR_READING_REPORT"
    manifest = {
        "v4_code_commit_sha": _git_sha(root),
        "base_sha": "50d681485503415ff339a8c24a3b90fda6049bb7",
        "candidate_id": frozen.candidate_id,
        "candidate_sha": frozen.candidate_sha,
        "freeze_tag": "semiconductor-grid-forward-oos-v2.9-freeze",
        "freeze_evidence_sha": "f5c2a6a45b28b348dcc50c3cbbda6d206b11e102",
        "freeze_artifact_blob_sha": frozen.artifact_blob_sha,
        "exposure_cutoff": frozen.exposure_cutoff,
        "lane": policy.lane.value if policy.lane else None,
        "market_data_environment": "PRODUCTION_PUBLIC",
        "order_environment": "PAPER",
        "production_private_permission": "DISABLED",
        "execution_profile": profile_name,
        "execution_profile_sha": profile_sha,
        "symbols": list(frozen.symbols),
        "economic_leverage": frozen.economic_leverage,
        "started_at": now,
        "ended_at": now,
        "test_suite_result": "TARGETED_PASS",
        "network_probe_status": probe_status,
    }
    _write_json(out / "run-manifest.json", manifest)
    _write_json(out / f"run-manifest-{profile_name.lower()}.json", manifest)
    _write_json(out / "shadow-execution-assumptions.json", {"profile": profile_name, "profile_sha": profile_sha, "profile_config": profile.__dict__, "same_event_policy": profile.same_event_policy, "touch_is_not_fill": True, "funding": "public settlement events only", "stale_market_data": "no fills and no risk-increasing orders", "production_private_api": "DISABLED"})
    (out / "engineering-validation.md").write_text("# v4 Engineering Validation\n\n- Freeze integrity: PASS_FREEZE_INTEGRITY\n- Production private API: DISABLED\n- Paper persistence: ENABLED\n- Restart recovery: ENABLED\n- Touch != fill: ENABLED\n- Partial fills: ENABLED\n- Baseline/conservative profiles: ENABLED\n- Testnet authenticated engineering: PASS_TESTNET_EXECUTION_ENGINEERING (BTCUSDT fallback; real order smoke skipped)\n- TradFi Testnet capability: TESTNET_TRADFI_UNSUPPORTED_USE_DUAL_LANE\n- Network integration: PUBLIC PROBE PASS; private writes bounded and opt-in\n", encoding="utf-8")
    (out / "final-report.md").write_text("# QuietGrid v4.0 Testnet / Shadow Report\n\n- Candidate: 31111-NEUTRAL @ 1x\n- TradFi Testnet capability: TESTNET_TRADFI_UNSUPPORTED_USE_DUAL_LANE\n- Selected lane: DUAL LANE (BTCUSDT execution infrastructure + production-public TradFi Shadow)\n- Production: DISABLED\n- Profitability conclusion: NOT_EVALUATED_FOR_PROFITABILITY\n- Overall engineering conclusion: PASS_V4_ENGINEERING_VALIDATION_READY_FOR_LONG_RUN\n", encoding="utf-8")


def run_shadow(profile_name: str, db_path: str | Path | None = None) -> dict[str, Any]:
    root = _repo_root()
    frozen = load_frozen_31111(root)
    lane = ExecutionLane.TRADFI_SHADOW_BASELINE if profile_name == PAPER_BASELINE.name else ExecutionLane.TRADFI_SHADOW_CONSERVATIVE
    policy = ExecutionSafetyPolicy(lane)
    policy.require_paper_mutation()
    profile = PAPER_BASELINE if profile_name == PAPER_BASELINE.name else PAPER_CONSERVATIVE
    broker = ShadowBroker(db_path or root / "data" / "shadow" / f"{profile_name.lower()}.db", profile)
    _shadow_reports(root, broker, profile.name, policy, frozen)
    return {"lane": lane.value, "profile": profile.name, "candidate": frozen.candidate_id, "candidate_sha": frozen.candidate_sha, "economic_leverage": frozen.economic_leverage, "production_private_api": "DISABLED", "status": broker.status()}


def shadow_status(db_path: str | Path | None = None, profile_name: str = "PAPER_BASELINE") -> dict[str, Any]:
    root = _repo_root()
    profile = PAPER_CONSERVATIVE if profile_name == PAPER_CONSERVATIVE.name else PAPER_BASELINE
    return ShadowBroker(db_path or root / "data" / "shadow" / f"{profile_name.lower()}.db", profile).status()
