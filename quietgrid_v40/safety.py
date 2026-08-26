from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class ExecutionLane(str, Enum):
    TESTNET_EXECUTION = "TESTNET_EXECUTION"
    TRADFI_SHADOW_BASELINE = "TRADFI_SHADOW_BASELINE"
    TRADFI_SHADOW_CONSERVATIVE = "TRADFI_SHADOW_CONSERVATIVE"
    PUBLIC_DATA_ONLY = "PUBLIC_DATA_ONLY"


class CapabilityStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    SKIPPED_NO_CREDENTIALS = "SKIPPED_NO_CREDENTIALS"
    SKIPPED_NOT_REQUESTED = "SKIPPED_NOT_REQUESTED"
    BLOCKED_SAFETY = "BLOCKED_SAFETY"
    ERROR_RETRYABLE = "ERROR_RETRYABLE"
    ERROR_FATAL = "ERROR_FATAL"


@dataclass(frozen=True)
class CapabilityMatrix:
    production_public_rest: bool
    production_public_websocket: bool
    production_signed_rest: bool
    testnet_signed_rest: bool
    testnet_websocket: bool
    paper_order_mutation: bool
    real_order_mutation: bool

    def to_mapping(self) -> dict[str, bool]:
        return {
            "production_public_rest": self.production_public_rest,
            "production_public_websocket": self.production_public_websocket,
            "production_signed_rest": self.production_signed_rest,
            "testnet_signed_rest": self.testnet_signed_rest,
            "testnet_websocket": self.testnet_websocket,
            "paper_order_mutation": self.paper_order_mutation,
            "real_order_mutation": self.real_order_mutation,
        }


class ProductionPrivateApiBlocked(PermissionError):
    """Raised whenever v4 would reach a production private endpoint."""


class LaneConfigurationError(ValueError):
    pass


class ExecutionSafetyPolicy:
    """Central fail-closed policy for v4 network and order capabilities."""

    _TESTNET_REST_HOSTS = frozenset({"testnet.binancefuture.com", "testnet.binance.vision"})
    _TESTNET_WS_HOSTS = frozenset({"stream.binancefuture.com", "stream.binance.vision"})
    _PRODUCTION_PRIVATE_HOSTS = frozenset({"fapi.binance.com", "fstream.binance.com"})
    _PUBLIC_PATH_PREFIXES = (
        "/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/klines",
        "/fapi/v1/ticker", "/fapi/v1/depth", "/fapi/v1/aggTrades",
        "/fapi/v1/trades", "/fapi/v1/premiumIndex", "/fapi/v1/fundingRate",
    )

    def __init__(self, lane: ExecutionLane | str | None, *, testnet_env: str | None = None,
                 rest_url: str | None = None, ws_url: str | None = None) -> None:
        self.lane = self._coerce_lane(lane)
        self.testnet_env = testnet_env
        self.rest_url = rest_url or ""
        self.ws_url = ws_url or ""

    @staticmethod
    def _coerce_lane(lane: ExecutionLane | str | None) -> ExecutionLane | None:
        if lane is None or str(lane).strip() == "":
            return None
        try:
            return lane if isinstance(lane, ExecutionLane) else ExecutionLane(str(lane).upper())
        except ValueError as exc:
            raise LaneConfigurationError(f"unknown execution lane: {lane}") from exc

    @staticmethod
    def _host(url: str) -> str:
        return urlparse(url).hostname or ""

    def capability_matrix(self) -> CapabilityMatrix:
        lane = self.lane
        return CapabilityMatrix(
            production_public_rest=lane in {
                ExecutionLane.TRADFI_SHADOW_BASELINE,
                ExecutionLane.TRADFI_SHADOW_CONSERVATIVE,
                ExecutionLane.PUBLIC_DATA_ONLY,
            },
            production_public_websocket=lane in {
                ExecutionLane.TRADFI_SHADOW_BASELINE,
                ExecutionLane.TRADFI_SHADOW_CONSERVATIVE,
                ExecutionLane.PUBLIC_DATA_ONLY,
            },
            production_signed_rest=False,
            testnet_signed_rest=(
                lane is ExecutionLane.TESTNET_EXECUTION
                and str(self.testnet_env or "").strip().lower() == "true"
                and self._host(self.rest_url) in self._TESTNET_REST_HOSTS
            ),
            testnet_websocket=(
                lane is ExecutionLane.TESTNET_EXECUTION
                and self._host(self.ws_url) in self._TESTNET_WS_HOSTS
            ),
            paper_order_mutation=lane in {
                ExecutionLane.TRADFI_SHADOW_BASELINE,
                ExecutionLane.TRADFI_SHADOW_CONSERVATIVE,
            },
            real_order_mutation=False,
        )

    def require_public_read(self, endpoint: str) -> None:
        parsed = urlparse(endpoint)
        host = parsed.hostname or ""
        path = parsed.path.rstrip("/") or "/"
        is_public_path = any(path == prefix or path.startswith(prefix + "/") for prefix in self._PUBLIC_PATH_PREFIXES)
        if host in self._PRODUCTION_PRIVATE_HOSTS and not is_public_path:
            raise ProductionPrivateApiBlocked("production private API is permanently disabled in v4")
        if self.lane is None:
            raise LaneConfigurationError("missing lane => NO_ORDER_CAPABILITY")

    def require_testnet_write(self, *, endpoint: str | None = None, allow_order_smoke: bool = False) -> None:
        if self.lane is not ExecutionLane.TESTNET_EXECUTION:
            raise ProductionPrivateApiBlocked("testnet signed writes require TESTNET_EXECUTION lane")
        if str(self.testnet_env or "").strip().lower() != "true":
            raise ProductionPrivateApiBlocked("BINANCE_TESTNET=true is required for testnet writes")
        if endpoint and self._host(endpoint) not in self._TESTNET_REST_HOSTS:
            raise ProductionPrivateApiBlocked("signed endpoint is not an allowed Binance Futures Testnet host")
        if allow_order_smoke is False:
            return

    def require_paper_mutation(self) -> None:
        if self.lane not in {
            ExecutionLane.TRADFI_SHADOW_BASELINE,
            ExecutionLane.TRADFI_SHADOW_CONSERVATIVE,
        }:
            raise LaneConfigurationError("paper order mutation requires an explicit TRADFI_SHADOW lane")

    def describe(self) -> dict[str, object]:
        matrix = self.capability_matrix()
        if self.lane is ExecutionLane.TESTNET_EXECUTION:
            data_environment = "TESTNET_PUBLIC"
            order_environment = "TESTNET_SIGNED"
        elif self.lane in {ExecutionLane.TRADFI_SHADOW_BASELINE, ExecutionLane.TRADFI_SHADOW_CONSERVATIVE, ExecutionLane.PUBLIC_DATA_ONLY}:
            data_environment = "PRODUCTION_PUBLIC"
            order_environment = "PAPER" if self.lane is not ExecutionLane.PUBLIC_DATA_ONLY else "NONE"
        else:
            data_environment = "NONE"
            order_environment = "NONE"
        return {
            "lane": self.lane.value if self.lane else None,
            "data_environment": data_environment,
            "order_environment": order_environment,
            "candidate": "31111-NEUTRAL",
            "economic_leverage": 1,
            "production_private_api": "DISABLED",
            "capabilities": matrix.to_mapping(),
        }
