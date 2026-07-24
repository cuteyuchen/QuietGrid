"""Shared utilities for the stock-perpetual weekend research protocol.

The research scripts deliberately keep their network and file-boundary logic in
one small module.  This makes checksum verification, immutable writes and test
fixtures consistent across discovery, freezing and audit stages.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# Allow ``python scripts/<name>.py`` as well as ``python -m scripts.<name>``.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_sources.archive_checksum import (  # noqa: E402
    parse_checksum_file,
    sha256_hexdigest,
    sha256_file,
    verify_official_checksum,
)
from data_sources.archive_funding_reader import read_archive_funding  # noqa: E402
from data_sources.archive_zip_reader import read_archive_klines  # noqa: E402
from data_sources.base import DataSourceError  # noqa: E402
from data_sources.binance_source import _httpx_proxy_kwargs  # noqa: E402
from data_sources.models import FundingEvent, NormalizedKline  # noqa: E402
from exchange.binance import symbol_rules_from_exchange_info  # noqa: E402


UTC = timezone.utc
ARCHIVE_BASE_URL = "https://data.binance.vision"
ARCHIVE_S3_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
MARK_KLINES_URL = "https://fapi.binance.com/fapi/v1/markPriceKlines"
PREMIUM_KLINES_URL = "https://fapi.binance.com/fapi/v1/premiumIndexKlines"
AGG_TRADES_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
INTERVAL_MS = 60_000
SEED_VALUES = (3, 10, 17, 31, 59, 97)


class PublicDataError(RuntimeError):
    """A public endpoint or archive failed in a way that invalidates a freeze."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def iso_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()


def timestamp_ms(value: datetime | date) -> int:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区。")
    return int(value.astimezone(UTC).timestamp() * 1000)


def month_start(value: date) -> date:
    return value.replace(day=1)


def next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def month_sequence(start: date, end: date) -> list[date]:
    cursor = month_start(start)
    last = month_start(end)
    result: list[date] = []
    while cursor <= last:
        result.append(cursor)
        cursor = next_month(cursor)
    return result


def day_sequence(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"
    return result.stdout.strip() or "UNKNOWN"


def git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"
    return result.stdout.strip() or "UNKNOWN"


def load_proxy_config(proxy_url: str | None = None) -> dict[str, Any] | None:
    if proxy_url:
        return {"enabled": True, "http": proxy_url, "https": proxy_url}
    try:
        from core.config import load_config

        raw = load_config().raw.get("proxy")
    except Exception:
        raw = None
    return raw if isinstance(raw, dict) else None


class PublicHttpClient:
    """Small retrying client for public Binance and listing endpoints."""

    def __init__(
        self,
        *,
        proxy_config: dict[str, Any] | None = None,
        timeout_seconds: float = 90.0,
        retries: int = 8,
        pause_seconds: float = 0.05,
    ) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.retries = max(1, int(retries))
        self.pause_seconds = max(0.0, float(pause_seconds))
        kwargs = _httpx_proxy_kwargs(proxy_config)
        self.client = httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "QuietGrid/stock-perp-research-v2.5"},
            **kwargs,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "PublicHttpClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        expected: set[int] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.client.request(
                    method,
                    url,
                    params=params,
                    timeout=self.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(self.pause_seconds * (2**attempt))
                    continue
                break
            if expected and response.status_code in expected:
                return response
            if response.status_code in {429, 418} or response.status_code >= 500:
                last_error = PublicDataError(
                    f"公开接口暂时不可用: HTTP {response.status_code} {url}"
                )
                if attempt + 1 < self.retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else self.pause_seconds * (2**attempt)
                    except ValueError:
                        delay = self.pause_seconds * (2**attempt)
                    time.sleep(max(self.pause_seconds, delay))
                    continue
                break
            if response.status_code >= 400:
                body = response.text[:300].replace("\n", " ")
                raise PublicDataError(
                    f"公开接口请求失败: HTTP {response.status_code} {url}: {body}"
                )
            return response
        raise PublicDataError(f"公开接口重试耗尽: {url}: {last_error}") from last_error

    def json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        response = self.request("GET", url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise PublicDataError(f"公开接口返回无效 JSON: {url}") from exc

    def bytes(self, url: str, *, params: Mapping[str, Any] | None = None) -> bytes:
        return self.request("GET", url, params=params).content

    def head(self, url: str) -> bool:
        response = self.request("HEAD", url, expected={404})
        return response.status_code < 400


def exchange_info(client: PublicHttpClient, url: str = EXCHANGE_INFO_URL) -> dict[str, Any]:
    payload = client.json(url)
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise PublicDataError("exchangeInfo 缺少 symbols 数组。")
    return payload


def parse_exchange_rule_snapshot(symbol_info: Mapping[str, Any]) -> dict[str, Any]:
    rules = symbol_rules_from_exchange_info(dict(symbol_info))
    filters = [dict(item) for item in symbol_info.get("filters", []) if isinstance(item, dict)]
    return {
        "tick_size": rules["tick_size"],
        "step_size": rules["step_size"],
        "min_qty": rules["min_qty"],
        "min_notional": rules["min_notional"],
        "price_precision": symbol_info.get("pricePrecision"),
        "quantity_precision": symbol_info.get("quantityPrecision"),
        "base_asset_precision": symbol_info.get("baseAssetPrecision"),
        "quote_precision": symbol_info.get("quotePrecision"),
        "filters": filters,
        "order_types": list(symbol_info.get("orderTypes") or []),
        "time_in_force": list(symbol_info.get("timeInForce") or []),
        "max_move_order_limit": symbol_info.get("maxMoveOrderLimit"),
    }


def archive_url(key: str) -> str:
    return f"{ARCHIVE_BASE_URL}/{key.lstrip('/')}"


def list_s3_objects(
    client: PublicHttpClient,
    prefix: str,
    *,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """List public archive objects without relying on the HTML index page."""
    result: list[dict[str, Any]] = []
    token: str | None = None
    namespace = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    for _ in range(max_pages):
        params: dict[str, Any] = {
            "list-type": "2",
            "prefix": prefix,
            "max-keys": "1000",
        }
        if token:
            params["continuation-token"] = token
        response = client.request("GET", ARCHIVE_S3_URL, params=params)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise PublicDataError(f"S3 目录响应不是 XML: {prefix}") from exc
        for item in root.findall("s:Contents", namespace):
            key = item.findtext("s:Key", default="", namespaces=namespace)
            if not key:
                continue
            size_text = item.findtext("s:Size", default="0", namespaces=namespace)
            result.append(
                {
                    "key": key,
                    "size": int(size_text or 0),
                    "last_modified": item.findtext(
                        "s:LastModified", default="", namespaces=namespace
                    ),
                }
            )
        truncated = root.findtext("s:IsTruncated", default="false", namespaces=namespace)
        if truncated.lower() != "true":
            break
        token = root.findtext("s:NextContinuationToken", default=None, namespaces=namespace)
        if not token:
            break
    return result


def verified_archive(
    client: PublicHttpClient,
    key: str,
) -> tuple[bytes, dict[str, Any]]:
    """Download one ZIP and verify the adjacent official CHECKSUM."""
    url = archive_url(key)
    data = client.bytes(url)
    checksum_text = client.bytes(f"{url}.CHECKSUM").decode("utf-8-sig")
    official = parse_checksum_file(checksum_text)
    actual = verify_official_checksum(data, checksum_text)
    return data, {
        "url": url,
        "checksum_url": f"{url}.CHECKSUM",
        "official_checksum": official,
        "local_sha256": actual,
        "downloaded_at": utc_now().isoformat(),
        "zip_size_bytes": len(data),
    }


def immutable_write(path: Path, data: bytes | str) -> str:
    """Write once; an existing path is accepted only when bytes are identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    digest = sha256_hexdigest(payload)
    if path.exists():
        existing = path.read_bytes()
        existing_digest = sha256_hexdigest(existing)
        if existing_digest != digest:
            raise PublicDataError(
                f"不可变文件已存在且内容不同，拒绝覆盖: {path} "
                f"({existing_digest[:12]} != {digest[:12]})"
            )
        return digest
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(path)
    except Exception:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return digest


def write_json(path: Path, payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return immutable_write(path, text)


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    return immutable_write(path, buffer.getvalue())


def zip_single_csv(
    data: bytes,
    *,
    expected_csv_name: str,
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
) -> str:
    """Safely extract the single CSV used by sidecar archives."""
    import zipfile

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PublicDataError(f"归档 ZIP 损坏: {expected_csv_name}") from exc
    with archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) != 1 or entries[0].filename != expected_csv_name:
            names = [item.filename for item in entries]
            raise PublicDataError(
                f"归档必须只包含 {expected_csv_name}，实际为 {names}"
            )
        item = entries[0]
        if item.file_size > max_uncompressed_bytes:
            raise PublicDataError(f"归档解压体积超过上限: {expected_csv_name}")
        try:
            raw = archive.read(item)
        except zipfile.BadZipFile as exc:
            raise PublicDataError(f"归档 CSV CRC 校验失败: {expected_csv_name}") from exc
    if len(raw) > max_uncompressed_bytes:
        raise PublicDataError(f"归档解压体积超过上限: {expected_csv_name}")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PublicDataError(f"归档 CSV 不是 UTF-8: {expected_csv_name}") from exc


def parse_sidecar_rows(
    data: bytes,
    *,
    expected_csv_name: str,
    kind: str,
) -> list[dict[str, Any]]:
    text = zip_single_csv(data, expected_csv_name=expected_csv_name)
    reader = csv.reader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for line_no, fields in enumerate(reader, start=1):
        if not fields or not any(str(value).strip() for value in fields):
            continue
        first = str(fields[0]).strip().lower()
        if line_no == 1 and first in {"open_time", "agg_trade_id", "id"}:
            continue
        if kind in {"mark_price", "premium_index"}:
            if len(fields) < 5:
                raise PublicDataError(f"{kind} 第 {line_no} 行列数不足")
            try:
                open_time = normalize_timestamp(fields[0])
                close_time = normalize_timestamp(fields[6]) if len(fields) > 6 else open_time + INTERVAL_MS - 1
                rows.append(
                    {
                        "open_time": open_time,
                        "close_time": close_time,
                        "open": fields[1],
                        "high": fields[2],
                        "low": fields[3],
                        "close": fields[4],
                        "volume": fields[5] if len(fields) > 5 else "0",
                    }
                )
            except (TypeError, ValueError) as exc:
                raise PublicDataError(f"{kind} 第 {line_no} 行无效") from exc
        elif kind == "agg_trades":
            if len(fields) < 7:
                raise PublicDataError(f"aggTrades 第 {line_no} 行列数不足")
            try:
                rows.append(
                    {
                        "agg_trade_id": int(fields[0]),
                        "price": fields[1],
                        "quantity": fields[2],
                        "first_trade_id": int(fields[3]),
                        "last_trade_id": int(fields[4]),
                        "transact_time": normalize_timestamp(fields[5]),
                        "is_buyer_maker": str(fields[6]).strip().lower(),
                    }
                )
            except (TypeError, ValueError) as exc:
                raise PublicDataError(f"aggTrades 第 {line_no} 行无效") from exc
        else:
            raise ValueError(f"未知 sidecar 类型: {kind}")
    return rows


def normalize_timestamp(value: Any) -> int:
    number = int(float(str(value).strip()))
    while number >= 100_000_000_000_000:
        number //= 1000
    if not 100_000_000_000 <= number < 100_000_000_000_000:
        raise ValueError("时间戳必须为毫秒或可规范化的微秒")
    return number


def kline_rows_from_archive(data: bytes, *, expected_csv_name: str) -> list[NormalizedKline]:
    return read_archive_klines(
        data,
        expected_csv_name=expected_csv_name,
        max_uncompressed_bytes=2 * 1024 * 1024 * 1024,
    )


def funding_rows_from_archive(data: bytes, *, expected_csv_name: str) -> list[FundingEvent]:
    return read_archive_funding(
        data,
        expected_csv_name=expected_csv_name,
        max_uncompressed_bytes=256 * 1024 * 1024,
    )


def compact_file_meta(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def format_float(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return format(float(value), ".16g")
    except (TypeError, ValueError):
        return str(value)


def archive_month_from_key(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1]
    for part in name.replace(".zip", "").split("-"):
        if len(part) == 7 and part[4] == "-":
            try:
                datetime.strptime(part, "%Y-%m")
            except ValueError:
                continue
            return part
    return None


def archive_day_from_key(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1]
    for part in name.replace(".zip", "").split("-"):
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            try:
                datetime.strptime(part, "%Y-%m-%d")
            except ValueError:
                continue
            return part
    return None


def symbol_listing_key(base_asset: str) -> tuple[str, ...]:
    normalized = base_asset.upper().strip()
    variants = [normalized, normalized.replace(".", "")]
    if normalized == "BRKB":
        variants.extend(["BRK.B", "BRKB"])
    return tuple(dict.fromkeys(variants))


def fetch_us_listings(client: PublicHttpClient) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch Nasdaq Trader's two public symbol dictionaries."""
    mapping: dict[str, dict[str, Any]] = {}
    sources: dict[str, Any] = {}
    for name, url in (("nasdaq", NASDAQ_LISTED_URL), ("other", OTHER_LISTED_URL)):
        raw = client.bytes(url)
        digest = sha256_hexdigest(raw)
        text = raw.decode("utf-8-sig").replace("\r", "")
        rows = list(csv.DictReader(io.StringIO(text), delimiter="|"))
        sources[name] = {
            "url": url,
            "sha256": digest,
            "downloaded_at": utc_now().isoformat(),
            "row_count": len(rows),
        }
        for row in rows:
            symbol = (row.get("Symbol") or row.get("ACT Symbol") or "").strip().upper()
            if not symbol or symbol == "FILE CREATION TIME":
                continue
            mapping[symbol] = dict(row)
            mapping.setdefault(symbol.replace(".", ""), dict(row))
    return mapping, sources


def lookup_listing(mapping: Mapping[str, Mapping[str, Any]], base_asset: str) -> dict[str, Any] | None:
    for key in symbol_listing_key(base_asset):
        row = mapping.get(key)
        if row:
            return dict(row)
    return None


def is_etf_listing(row: Mapping[str, Any] | None) -> bool:
    if not row:
        return False
    if str(row.get("ETF", "")).strip().upper() == "Y":
        return True
    name = str(row.get("Security Name", "")).upper()
    return " ETF" in name or " ETF " in name or name.endswith(" ETF")


def listing_exchange(row: Mapping[str, Any] | None) -> str | None:
    if not row:
        return None
    return str(row.get("Exchange") or row.get("Market Category") or "").strip() or None


def complete_month_count(first_ms: int | None, last_ms: int | None) -> int:
    if first_ms is None or last_ms is None or first_ms >= last_ms:
        return 0
    first = datetime.fromtimestamp(first_ms / 1000, tz=UTC).date()
    last = datetime.fromtimestamp(last_ms / 1000, tz=UTC).date()
    # A month is complete only when the observed range covers its full UTC date.
    first_month = month_start(first)
    if first.day != 1:
        first_month = next_month(first_month)
    last_month = month_start(last)
    next_day = last + timedelta(days=1)
    if next_day.month == last.month:
        last_month = month_start(last) - timedelta(days=1)
        last_month = month_start(last_month)
    if first_month > last_month:
        return 0
    return (last_month.year - first_month.year) * 12 + last_month.month - first_month.month + 1


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value
