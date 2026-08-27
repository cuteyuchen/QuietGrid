from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from exchange.public_market import ProductionPublicMarketData
from exchange.shadow import PAPER_BASELINE, PAPER_CONSERVATIVE, ShadowBroker
from quietgrid_v41.production_probe import probe, write_probe_reports
from quietgrid_v41.reports import write_v41_reports
from quietgrid_v41.runtime import BinancePublicTradeStream, ContinuousShadowRuntime
from quietgrid_v41.testnet import run_testnet_order_lifecycle


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


async def _run_lane(profile_name: str, max_events: int | None, max_seconds: float | None, db_path: str | None) -> dict:
    root = _root()
    profile = PAPER_CONSERVATIVE if profile_name == PAPER_CONSERVATIVE.name else PAPER_BASELINE
    market_data = ProductionPublicMarketData()
    runtime = ContinuousShadowRuntime.create(repo_root=root, market_data=market_data, profile=profile, db_path=db_path)
    source = BinancePublicTradeStream(runtime.frozen.symbols, rest_recovery=runtime.recover_from_rest)
    result = await runtime.run(source, max_events=max_events, max_seconds=max_seconds, bootstrap_rest=True)
    write_v41_reports(root, runtime=result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="QuietGrid v4.1 bounded continuous shadow runtime")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--production-public-probe", action="store_true")
    modes.add_argument("--shadow-baseline", action="store_true")
    modes.add_argument("--shadow-conservative", action="store_true")
    modes.add_argument("--shadow-status", action="store_true")
    modes.add_argument("--shadow-reconcile", action="store_true")
    modes.add_argument("--v41-testnet-order-lifecycle", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--max-events", type=int)
    parser.add_argument("--max-seconds", type=float, default=15.0)
    parser.add_argument("--db-path")
    args = parser.parse_args()
    if args.max_events is not None and args.max_events <= 0:
        parser.error("--max-events must be positive")
    if args.max_seconds is not None and args.max_seconds <= 0:
        parser.error("--max-seconds must be positive")
    root = _root()
    if args.production_public_probe:
        result = asyncio.run(probe(network=not args.no_network))
        write_probe_reports(result, root / "reports" / "testnet-shadow-v4.1")
    elif args.v41_testnet_order_lifecycle:
        from core.config import load_config

        result = asyncio.run(run_testnet_order_lifecycle(load_config()))
        write_v41_reports(root, testnet=result)
    elif args.shadow_status or args.shadow_reconcile:
        profile = PAPER_CONSERVATIVE if args.shadow_conservative else PAPER_BASELINE
        broker = ShadowBroker(args.db_path or root / "data" / "runtime" / "v41" / f"shadow-{profile.name.lower()}.sqlite", profile)
        result = broker.status()
        if args.shadow_reconcile:
            result = asyncio.run(broker.reconcile())
    else:
        profile_name = PAPER_CONSERVATIVE.name if args.shadow_conservative else PAPER_BASELINE.name
        result = asyncio.run(_run_lane(profile_name, args.max_events, args.max_seconds, args.db_path))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
