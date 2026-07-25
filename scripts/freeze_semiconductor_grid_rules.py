"""Freeze Binance rule snapshots for the v2.7 semiconductor grid universe.

This command uses only the public USD-M ``exchangeInfo`` endpoint.  It does not
need API credentials and never performs account or order actions.  The output is
consumed by ``scripts/semiconductor_grid_backtest.py`` so price and quantity
rounding, minimum quantity and minimum notional are reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx
import yaml


EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 SNDK/MU/SOXL/SKHYNIX Binance USD-M 交易规则",
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument(
        "--output",
        default="data/backtests/semiconductor-v2.7/exchange-rules.json",
    )
    parser.add_argument("--exchange-info-url", default=EXCHANGE_INFO_URL)
    parser.add_argument("--proxy-url", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    config_path = Path(args.config)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    symbols = tuple(
        str(value).strip().upper()
        for value in (raw.get("semiconductor_grid", {}) or {}).get(
            "research_symbols", []
        )
        if str(value).strip()
    )
    if not symbols:
        raise ValueError("配置没有 semiconductor_grid.research_symbols。")

    proxy = args.proxy_url or _configured_proxy(raw.get("proxy", {}) or {})
    client_kwargs: dict[str, Any] = {
        "timeout": max(1.0, float(args.timeout_seconds)),
        "follow_redirects": True,
        "headers": {"User-Agent": "QuietGrid/semiconductor-grid-v2.7"},
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        response = client.get(args.exchange_info_url)
        response.raise_for_status()
        payload_bytes = response.content
        payload = response.json()

    records = extract_rule_snapshots(payload, symbols)
    missing = sorted(set(symbols) - set(records))
    if missing:
        raise RuntimeError(
            "exchangeInfo 缺少或未开放以下研究标的: " + ", ".join(missing)
        )
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_url": args.exchange_info_url,
        "source_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "symbols": records,
    }
    path = Path(args.output)
    if path.exists() and not args.overwrite:
        raise FileExistsError(f"规则快照已存在: {path}；传 --overwrite 才能替换。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(path),
                "symbols": list(records),
                "sha256": _sha256_file(path),
            },
            ensure_ascii=False,
        )
    )


def extract_rule_snapshots(
    payload: Mapping[str, Any],
    symbols: tuple[str, ...] | list[str],
) -> dict[str, dict[str, Any]]:
    requested = {str(value).strip().upper() for value in symbols}
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list):
        raise ValueError("exchangeInfo 缺少 symbols 数组。")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_symbols:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        if symbol not in requested:
            continue
        filters = {
            str(item.get("filterType") or ""): item
            for item in raw.get("filters", [])
            if isinstance(item, Mapping)
        }
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        notional_filter = filters.get("MIN_NOTIONAL", {})
        tick_size = _positive_number(price_filter.get("tickSize"), "tickSize")
        step_size = _positive_number(lot_filter.get("stepSize"), "stepSize")
        min_qty = _non_negative_number(lot_filter.get("minQty"), "minQty")
        min_notional = _non_negative_number(
            notional_filter.get("notional", notional_filter.get("minNotional", 0)),
            "minNotional",
        )
        result[symbol] = {
            "status": str(raw.get("status") or ""),
            "contract_type": str(raw.get("contractType") or ""),
            "onboard_date": int(raw.get("onboardDate") or 0),
            "base_asset": str(raw.get("baseAsset") or ""),
            "quote_asset": str(raw.get("quoteAsset") or ""),
            "tick_size": tick_size,
            "step_size": step_size,
            "min_qty": min_qty,
            "min_notional": min_notional,
            "price_precision": raw.get("pricePrecision"),
            "quantity_precision": raw.get("quantityPrecision"),
            "order_types": list(raw.get("orderTypes") or []),
            "time_in_force": list(raw.get("timeInForce") or []),
        }
    return result


def _configured_proxy(raw: Mapping[str, Any]) -> str | None:
    if not bool(raw.get("enabled")):
        return None
    value = raw.get("https") or raw.get("http")
    return str(value).strip() if value else None


def _positive_number(value: Any, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} 必须为正数。")
    return number


def _non_negative_number(value: Any, name: str) -> float:
    number = float(value or 0)
    if number < 0:
        raise ValueError(f"{name} 不能为负数。")
    return number


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
