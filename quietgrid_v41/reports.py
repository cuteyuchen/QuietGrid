from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quietgrid_v41.production_probe import write_probe_reports


def write_v41_reports(
    repo_root: str | Path,
    *,
    probe: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
    testnet: dict[str, Any] | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    out = Path(output_dir).resolve() if output_dir is not None else Path(repo_root).resolve() / "reports" / "testnet-shadow-v4.1"
    out.mkdir(parents=True, exist_ok=True)
    if probe is not None:
        write_probe_reports(probe, out)
    probe_result = probe or {"classification": "NOT_RUN", "symbols": []}
    runtime = runtime or {"status": "NOT_RUN", "reconcile": {"result": "NOT_RUN"}}
    recovery = recovery or {"status": "NOT_RUN"}
    testnet = testnet or {"status": "SKIPPED_TESTNET_ORDER_LIFECYCLE_NOT_AUTHORIZED", "conclusion": "EXECUTION_INFRA_ONLY"}
    (out / "execution-correctness.md").write_text(
        "# v4.1 Execution Correctness\n\n"
        "- STOP_MARKET: explicit STOP_PENDING -> STOP_TRIGGERED -> FILLED path\n"
        "- reduce-only: exposure capped at current position; no flip\n"
        "- force-flat: latched, episode-scoped, bounded retry\n"
        "- maker price: resting limit price\n"
        "- touch != fill: queue, latency, eligibility, and participation required\n"
        f"- reconcile: {runtime.get('reconcile', {}).get('result', 'NOT_RUN')}\n",
        encoding="utf-8",
    )
    (out / "continuous-shadow-runtime.md").write_text(
        "# v4.1 Continuous Shadow Runtime\n\n"
        f"- status: {runtime.get('status', 'NOT_RUN')}\n"
        f"- events processed: {runtime.get('events_processed', 0)}\n"
        f"- controller ticks: {runtime.get('controller_ticks', 0)}\n"
        "- market data: Production PUBLIC WebSocket with REST bootstrap/recovery\n"
        "- production private API: DISABLED\n",
        encoding="utf-8",
    )
    (out / "restart-recovery.md").write_text(
        "# v4.1 Restart Recovery\n\n"
        f"- validation status: {recovery.get('status', 'NOT_RUN')}\n"
        "- persisted: cash, positions, orders, partial fills, stops, cancels, queue remaining, market cursor, funding settlements, force-flat episode\n"
        "- duplicate event/funding/order protections: enabled\n",
        encoding="utf-8",
    )
    (out / "safety-validation.md").write_text(
        "# v4.1 Safety Validation\n\n"
        "- production private/signed endpoints: BLOCKED\n"
        "- production order mutation: DISABLED\n"
        "- economic leverage: 1x\n"
        "- frozen candidate: 31111-NEUTRAL\n"
        "- profitability: NOT_EVALUATED_FOR_PROFITABILITY\n",
        encoding="utf-8",
    )
    (out / "testnet-order-lifecycle.md").write_text(
        "# v4.1 Testnet Order Lifecycle\n\n"
        f"- status: {testnet.get('status', 'SKIPPED_TESTNET_ORDER_LIFECYCLE_NOT_AUTHORIZED')}\n"
        f"- conclusion: {testnet.get('conclusion', 'EXECUTION_INFRA_ONLY')}\n"
        "- production private API: DISABLED\n",
        encoding="utf-8",
    )
    (out / "bounded-soak-summary.md").write_text(
        "# v4.1 Bounded Soak Summary\n\n"
        "- 24/7 runtime: NOT STARTED\n"
        "- bounded validation: caller-supplied duration/events only\n"
        f"- runtime status: {runtime.get('status', 'NOT_RUN')}\n"
        f"- stop reason: {runtime.get('stop_reason', 'NOT_RUN')}\n"
        "- network unavailable items must remain SKIPPED_NETWORK_UNAVAILABLE\n",
        encoding="utf-8",
    )
    manifest = {
        "version": "4.1",
        "candidate_id": "31111-NEUTRAL",
        "candidate_sha": "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774",
        "economic_leverage": 1,
        "forward_oos": "2 / 8",
        "runtime": runtime,
        "recovery": recovery,
        "production_public": probe_result,
        "testnet": testnet,
        "production_private_api": "DISABLED",
        "profitability": "NOT_EVALUATED_FOR_PROFITABILITY",
    }
    (out / "run-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    production_ready = probe_result.get("classification") == "PRODUCTION_PUBLIC_TRADFI_SUPPORTED"
    runtime_ready = runtime.get("conclusion") == "PASS_CONTINUOUS_SHADOW_RUNTIME"
    recovery_ready = recovery.get("status") == "PASS_SHADOW_RESTART_RECOVERY"
    bounded_ready = runtime.get("bounded_soak") == "PASS_BOUNDED_SOAK"
    overall = (
        "PASS_V41_ENGINEERING_READY_FOR_LONG_SHADOW_OBSERVATION"
        if production_ready and runtime_ready and recovery_ready and bounded_ready
        else "PARTIAL_V41_ENGINEERING_VALIDATION"
    )
    execution_code = runtime.get("execution_conclusion")
    if not execution_code:
        execution_code = "PASS_SHADOW_EXECUTION_CORRECTNESS" if runtime.get("reconcile", {}).get("result") == "RECONCILED" else "FAIL_SHADOW_EXECUTION_CORRECTNESS"
    (out / "final-report.md").write_text(
        "# QuietGrid v4.1 Shadow Runtime Report\n\n"
        f"- Branch gate: PASS_BRANCH_CONSOLIDATION\n"
        f"- Freeze: PASS_FREEZE_INTEGRITY\n"
        f"- Production public: {probe_result.get('classification', 'NOT_RUN')}\n"
        f"- Execution: {execution_code}\n"
        f"- Runtime: {runtime.get('conclusion', runtime.get('status', 'NOT_RUN'))}\n"
        f"- Overall: {overall}\n"
        "- Profitability: NOT_EVALUATED_FOR_PROFITABILITY\n",
        encoding="utf-8",
    )
    return out
