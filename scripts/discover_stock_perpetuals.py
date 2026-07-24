"""Discover and preliminarily classify Binance US stock perpetuals.

The symbol universe is derived from the live public ``exchangeInfo`` response
and Nasdaq Trader's public symbol dictionaries.  No configured symbol allowlist
is used as evidence that a contract exists.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

# Support both ``python scripts/name.py`` and module execution.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stock_perp_common import (
    AGG_TRADES_URL,
    EXCHANGE_INFO_URL,
    FUNDING_RATE_URL,
    INTERVAL_MS,
    KLINES_URL,
    PublicDataError,
    PublicHttpClient,
    complete_month_count,
    exchange_info,
    fetch_us_listings,
    git_branch,
    git_commit,
    iso_ms,
    is_etf_listing,
    listing_exchange,
    load_proxy_config,
    lookup_listing,
    month_start,
    next_month,
    parse_datetime,
    parse_exchange_rule_snapshot,
    immutable_write,
    write_json,
)


UTC = timezone.utc
REPORT_DIR = Path("reports/stock-perp-weekend-grid-v1")
_MONTH_RE = re.compile(r"(\d{4}-\d{2})(?:\.zip)?$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="发现 Binance 美股相关永续合约")
    parser.add_argument("--output-dir", default=str(REPORT_DIR))
    parser.add_argument("--exchange-info-url", default=EXCHANGE_INFO_URL)
    parser.add_argument("--proxy-url", default=None)
    parser.add_argument("--as-of-utc", default=None)
    parser.add_argument(
        "--data-previously-viewed",
        action="store_true",
        help="将本次探索前已查看过的公开数据明确标记为已查看",
    )
    parser.add_argument("--skip-rest-probes", action="store_true")
    return parser


def _month_from_key(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1]
    match = _MONTH_RE.search(name)
    return match.group(1) if match else None


def _asof_bounds(exchange_payload: Mapping[str, Any], override: str | None) -> tuple[datetime, int]:
    if override:
        asof = parse_datetime(override)
    else:
        raw = exchange_payload.get("serverTime")
        asof = (
            datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
            if raw not in (None, "")
            else datetime.now(UTC)
        )
    # The current UTC date is never considered complete.
    last_complete_date = asof.date() - timedelta(days=1)
    end = datetime.combine(last_complete_date, datetime.max.time(), tzinfo=UTC)
    return asof, int(end.timestamp() * 1000)


def _last_valid_kline(rows: Any, *, end_ms: int) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    valid: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            continue
        try:
            open_time = int(row[0])
            close_time = int(row[6])
            volume = float(row[5])
            trade_count = int(float(row[8]))
        except (TypeError, ValueError):
            continue
        if close_time < end_ms and open_time < end_ms:
            valid.append(
                {
                    "open_time": open_time,
                    "close_time": close_time,
                    "volume": volume,
                    "trade_count": trade_count,
                }
            )
    return valid[-1] if valid else None


def _first_valid_kline(rows: Any) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            continue
        try:
            open_time = int(row[0])
            close_time = int(row[6])
            volume = float(row[5])
            trade_count = int(float(row[8]))
        except (TypeError, ValueError):
            continue
        return {
            "open_time": open_time,
            "close_time": close_time,
            "volume": volume,
            "trade_count": trade_count,
        }
    return None


def _rest_probe(
    client: PublicHttpClient,
    symbol: str,
    onboard_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "first_valid_1m": None,
        "last_complete_1m": None,
        "weekend_rows": 0,
        "weekend_nonzero_rows": 0,
        "funding_event_count": 0,
        "funding_interval_hours": None,
        "errors": [],
    }
    first_end = min(end_ms, onboard_ms + 7 * 24 * 60 * 60 * 1000)
    try:
        first_payload = client.json(
            KLINES_URL,
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": onboard_ms,
                "endTime": first_end,
                "limit": 1000,
            },
        )
        result["first_valid_1m"] = _first_valid_kline(first_payload)
    except Exception as exc:
        result["errors"].append(f"first_klines: {exc}")
    last_start = max(onboard_ms, end_ms - 7 * 24 * 60 * 60 * 1000)
    try:
        last_payload = client.json(
            KLINES_URL,
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": last_start,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        result["last_complete_1m"] = _last_valid_kline(last_payload, end_ms=end_ms)
        # Probe several complete Saturday/Sunday spans.  A 1m request is capped
        # at 1,000 rows, so probing the latest seven-day prefix can miss the
        # weekend entirely; anchoring at Saturday avoids that look-through.
        end_date = datetime.fromtimestamp((end_ms - 1) / 1000, tz=UTC).date()
        days_since_saturday = (end_date.weekday() - 5) % 7
        saturday = end_date - timedelta(days=days_since_saturday)
        seen_weekend: set[int] = set()
        for offset in (0, 7, 14):
            start = datetime.combine(saturday - timedelta(days=offset), datetime.min.time(), tzinfo=UTC)
            start_ms = max(onboard_ms, int(start.timestamp() * 1000))
            weekend_payload = client.json(
                KLINES_URL,
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": start_ms,
                    "endTime": min(end_ms, start_ms + 2 * 24 * 60 * 60 * 1000),
                    "limit": 1000,
                },
            )
            for row in weekend_payload if isinstance(weekend_payload, list) else []:
                if not isinstance(row, list) or len(row) < 9:
                    continue
                try:
                    open_time = int(row[0])
                    close_time = int(row[6])
                    ts = datetime.fromtimestamp(open_time / 1000, tz=UTC)
                    nonzero = float(row[5]) > 0 or int(float(row[8])) > 0
                except (TypeError, ValueError):
                    continue
                if close_time < end_ms and ts.weekday() >= 5 and open_time not in seen_weekend:
                    seen_weekend.add(open_time)
                    result["weekend_rows"] += 1
                    result["weekend_nonzero_rows"] += int(nonzero)
    except Exception as exc:
        result["errors"].append(f"last_klines: {exc}")

    funding_start = max(onboard_ms, end_ms - 45 * 24 * 60 * 60 * 1000)
    try:
        funding_payload = client.json(
            FUNDING_RATE_URL,
            params={
                "symbol": symbol,
                "startTime": funding_start,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        times = sorted(
            int(row["fundingTime"])
            for row in funding_payload
            if isinstance(row, dict) and row.get("fundingTime") not in (None, "")
        )
        result["funding_event_count"] = len(times)
        if len(times) >= 2:
            hours = [(right - left) / 3_600_000 for left, right in zip(times, times[1:])]
            result["funding_interval_hours"] = round(statistics.median(hours), 3)
        if times:
            result["funding_first_time"] = times[0]
            result["funding_last_time"] = times[-1]
    except Exception as exc:
        result["errors"].append(f"funding: {exc}")
    return result


def _archive_listing_probe(
    client: PublicHttpClient,
    symbol: str,
) -> dict[str, Any]:
    monthly_prefix = f"data/futures/um/monthly/klines/{symbol}/1m/"
    daily_prefix = f"data/futures/um/daily/klines/{symbol}/1m/"
    result: dict[str, Any] = {
        "monthly_archive_count": 0,
        "monthly_archive_months": [],
        "daily_archive_count": 0,
        "first_archive_month": None,
        "last_archive_month": None,
        "errors": [],
    }
    # PublicHttpClient intentionally keeps S3 listing as a module function; this
    # local import avoids a second network abstraction in the discovery script.
    from scripts.stock_perp_common import list_s3_objects

    try:
        monthly = list_s3_objects(client, monthly_prefix)
        months = sorted(
            {
                value
                for item in monthly
                if str(item.get("key", "")).endswith(".zip")
                for value in [_month_from_key(str(item["key"]))]
                if value
            }
        )
        result["monthly_archive_count"] = len(months)
        result["monthly_archive_months"] = months
        if months:
            result["first_archive_month"] = months[0]
            result["last_archive_month"] = months[-1]
    except Exception as exc:
        result["errors"].append(f"monthly_archive: {exc}")
    try:
        daily = list_s3_objects(client, daily_prefix)
        result["daily_archive_count"] = sum(
            1
            for item in daily
            if str(item.get("key", "")).endswith(".zip")
        )
    except Exception as exc:
        result["errors"].append(f"daily_archive: {exc}")
    return result


def _calendar_weekend_estimate(first_ms: int | None, last_ms: int | None) -> int:
    if first_ms is None or last_ms is None:
        return 0
    start = datetime.fromtimestamp(first_ms / 1000, tz=UTC).date()
    end = datetime.fromtimestamp(last_ms / 1000, tz=UTC).date()
    # This is a conservative discovery estimate.  The frozen window manifest
    # performs the authoritative NYSE calendar count with Scheduler.
    cursor = start
    count = 0
    while cursor <= end:
        if cursor.weekday() == 4:  # each Friday approximates one weekend window
            count += 1
        cursor += timedelta(days=1)
    return count


def _classify(
    info: Mapping[str, Any],
    listing: Mapping[str, Any] | None,
    probe: Mapping[str, Any],
    *,
    complete_months: int,
    weekend_estimate: int,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if info.get("status") != "TRADING":
        reasons.append("status_not_trading")
    if info.get("contractType") not in {"PERPETUAL", "TRADIFI_PERPETUAL"}:
        reasons.append("not_perpetual")
    if info.get("quoteAsset") != "USDT":
        reasons.append("not_usdt_margined")
    if info.get("underlyingType") != "EQUITY":
        reasons.append("not_us_equity_underlying")
    if listing is None:
        reasons.append("no_nasdaq_or_nyse_listing_match")
    elif is_etf_listing(listing):
        reasons.append("listed_security_is_etf_not_company_stock")
    if probe.get("first_valid_1m") is None:
        reasons.append("no_first_valid_1m_probe")
    if probe.get("last_complete_1m") is None:
        reasons.append("no_last_complete_1m_probe")
    if not probe.get("weekend_nonzero_rows"):
        reasons.append("no_nonzero_weekend_probe")
    if probe.get("funding_event_count", 0) <= 0:
        reasons.append("no_funding_probe")
    if reasons:
        # A symbol with a valid economic mapping but too little history remains
        # descriptive Tier A-Short; hard exclusions stay EXCLUDED.
        hard = {
            "status_not_trading",
            "not_perpetual",
            "not_usdt_margined",
            "not_us_equity_underlying",
            "no_nasdaq_or_nyse_listing_match",
            "listed_security_is_etf_not_company_stock",
        }
        if any(item in hard for item in reasons):
            return "EXCLUDED", reasons
        return "TIER_A_SHORT", reasons
    if complete_months >= 4 and weekend_estimate >= 14:
        return "TIER_A_CORE", reasons
    return "TIER_A_SHORT", ["insufficient_complete_months_or_weekends"]


def _symbol_record(
    client: PublicHttpClient,
    info: Mapping[str, Any],
    listings: Mapping[str, Mapping[str, Any]],
    *,
    end_ms: int,
    skip_rest: bool,
) -> dict[str, Any]:
    symbol = str(info.get("symbol") or "").upper()
    base = str(info.get("baseAsset") or "").upper()
    listing = lookup_listing(listings, base)
    probe = {
        "first_valid_1m": None,
        "last_complete_1m": None,
        "weekend_rows": 0,
        "weekend_nonzero_rows": 0,
        "funding_event_count": 0,
        "funding_interval_hours": None,
        "errors": ["rest_probes_skipped"] if skip_rest else [],
    }
    if not skip_rest and info.get("onboardDate") not in (None, ""):
        probe = _rest_probe(client, symbol, int(info["onboardDate"]), end_ms)
    archive = _archive_listing_probe(client, symbol)
    first = probe.get("first_valid_1m") or {}
    last = probe.get("last_complete_1m") or {}
    first_ms = first.get("open_time")
    last_ms = last.get("open_time")
    complete_months = complete_month_count(first_ms, last_ms)
    weekend_estimate = _calendar_weekend_estimate(first_ms, last_ms)
    tier, reasons = _classify(
        info,
        listing,
        probe,
        complete_months=complete_months,
        weekend_estimate=weekend_estimate,
    )
    return {
        "symbol": symbol,
        "pair": info.get("pair"),
        "base_asset": base,
        "quote_asset": info.get("quoteAsset"),
        "margin_asset": info.get("marginAsset"),
        "underlying_type": info.get("underlyingType"),
        "underlying_sub_type": list(info.get("underlyingSubType") or []),
        "contract_type": info.get("contractType"),
        "status": info.get("status"),
        "onboard_date": iso_ms(int(info["onboardDate"])) if info.get("onboardDate") else None,
        "delivery_date": iso_ms(int(info["deliveryDate"])) if info.get("deliveryDate") else None,
        "rules": parse_exchange_rule_snapshot(info),
        "funding_interval_hours_probe": probe.get("funding_interval_hours"),
        "funding_event_probe": {
            "count": probe.get("funding_event_count", 0),
            "first_time": iso_ms(probe.get("funding_first_time")),
            "last_time": iso_ms(probe.get("funding_last_time")),
        },
        "first_valid_1m": first,
        "last_complete_1m": last,
        "weekend_probe": {
            "rows": probe.get("weekend_rows", 0),
            "nonzero_rows": probe.get("weekend_nonzero_rows", 0),
            "has_valid_trade": bool(probe.get("weekend_nonzero_rows", 0)),
        },
        "archive_probe": archive,
        "complete_months_estimate": complete_months,
        "weekend_windows_estimate": weekend_estimate,
        "listing": listing,
        "listing_exchange": listing_exchange(listing),
        "listing_is_etf": is_etf_listing(listing),
        "tier": tier,
        "exclusion_reasons": reasons,
        "probe_errors": list(probe.get("errors") or []) + list(archive.get("errors") or []),
    }


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Binance 美股相关永续合约发现",
        "",
        f"- 发现时间（UTC）：`{payload['as_of_utc']}`",
        f"- 官方 exchangeInfo：`{payload['exchange_info_url']}`",
        f"- 发现分支：`{payload['git_branch']}`",
        f"- 发现 Git：`{payload['git_commit']}`",
        f"- 数据此前已查看：`{payload['data_previously_viewed']}`",
        "",
        "候选范围来自官方 exchangeInfo；NYSE/Nasdaq 映射来自 Nasdaq Trader 公共目录。"
        "本表的 Tier A-Core 是冻结前的保守预分类，最终等级以数据审计和窗口 manifest 为准。",
        "",
        "| Symbol | Underlying | Contract | Status | Onboard | Listing | Months(est.) | Weekends(est.) | Tier | Exclusion |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in payload["symbols"]:
        listing = row.get("listing") or {}
        listing_name = str(listing.get("Security Name") or "").replace("|", " ")
        reasons = ", ".join(row.get("exclusion_reasons") or [])
        lines.append(
            "| `{symbol}` | `{underlying}` | `{contract}` | `{status}` | `{onboard}` | "
            "{listing} | {months} | {weekends} | **{tier}** | {reasons} |".format(
                symbol=row.get("symbol"),
                underlying=row.get("underlying_type"),
                contract=row.get("contract_type"),
                status=row.get("status"),
                onboard=(row.get("onboard_date") or "")[:10],
                listing=listing_name or "-",
                months=row.get("complete_months_estimate", 0),
                weekends=row.get("weekend_windows_estimate", 0),
                tier=row.get("tier"),
                reasons=reasons or "-",
            )
        )
    lines.extend(
        [
            "",
            f"总 exchangeInfo 符号：`{payload['exchange_info_symbol_count']}`；",
            f"EQUITY/HK_EQUITY：`{payload['equity_symbol_count']}`；",
            f"预分类 Tier A-Core：`{payload['tier_counts'].get('TIER_A_CORE', 0)}`；",
            f"预分类 Tier A-Short：`{payload['tier_counts'].get('TIER_A_SHORT', 0)}`；",
            f"排除：`{payload['tier_counts'].get('EXCLUDED', 0)}`。",
            "",
        ]
    )
    return "\n".join(lines)


def discover(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    proxy = load_proxy_config(args.proxy_url)
    with PublicHttpClient(proxy_config=proxy, pause_seconds=0.05) as client:
        payload = exchange_info(client, args.exchange_info_url)
        asof, end_ms = _asof_bounds(payload, args.as_of_utc)
        listings, listing_sources = fetch_us_listings(client)
        raw_symbols = [item for item in payload["symbols"] if isinstance(item, dict)]
        records: list[dict[str, Any]] = []
        for info in sorted(raw_symbols, key=lambda item: str(item.get("symbol", ""))):
            if info.get("underlyingType") not in {"EQUITY", "HK_EQUITY"}:
                continue
            records.append(
                _symbol_record(
                    client,
                    info,
                    listings,
                    end_ms=end_ms,
                    skip_rest=args.skip_rest_probes,
                )
            )

    # Keep a canonical public metadata snapshot for later audit/reproduction.
    exchange_snapshot = {
        "url": args.exchange_info_url,
        "retrieved_at": asof.isoformat(),
        "payload": payload,
    }
    write_json(output_dir / "exchange-info.json", exchange_snapshot)
    write_json(
        output_dir / "us-listings.json",
        {"sources": listing_sources, "symbols": listings},
    )
    tier_counts: dict[str, int] = {}
    for item in records:
        tier = str(item["tier"])
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    result = {
        "schema_version": 1,
        "protocol": "docs/codex-stock-perp-weekend-grid-backtest-v2.5.md",
        "generated_at": asof.isoformat(),
        "as_of_utc": asof.isoformat(),
        "last_complete_utc_date": (asof.date() - timedelta(days=1)).isoformat(),
        "exchange_info_url": args.exchange_info_url,
        "exchange_info_server_time": payload.get("serverTime"),
        "exchange_info_symbol_count": len(raw_symbols),
        "equity_symbol_count": len(records),
        "listing_sources": listing_sources,
        "git_branch": git_branch(),
        "git_commit": git_commit(),
        "data_previously_viewed": bool(args.data_previously_viewed),
        "direction_mode": "NEUTRAL",
        "leverage": 1,
        "production_defaults_changed": False,
        "symbols": records,
        "tier_counts": tier_counts,
        "candidate_symbols": [
            item["symbol"] for item in records if item["tier"] in {"TIER_A_CORE", "TIER_A_SHORT"}
        ],
        "notes": [
            "预分类不替代冻结后的完整 1m 缺口、交易规则和窗口审计。",
            "HK_EQUITY、ETF、非 USDT 或无法映射到公开 US listing 的符号不进入 Tier A-Core。",
            "任何已查看数据都必须在后续 manifest 中保留 data_previously_viewed 标记。",
        ],
    }
    write_json(output_dir / "symbol-discovery.json", result)
    immutable_write(output_dir / "symbol-discovery.md", _markdown(result))
    return result


def main() -> None:
    args = _parser().parse_args()
    try:
        result = discover(args)
    except (PublicDataError, ValueError) as exc:
        raise SystemExit(f"合约发现失败：{exc}") from exc
    print(
        json.dumps(
            {
                "report": str((Path(args.output_dir) / "symbol-discovery.json").resolve()),
                "tier_counts": result["tier_counts"],
                "candidate_count": len(result["candidate_symbols"]),
                "data_previously_viewed": result["data_previously_viewed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
