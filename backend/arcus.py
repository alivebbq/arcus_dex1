"""Arcus perpetuals REST client.

Reference: https://docs.arcus.xyz/api-reference/authentication
All prices/sizes remain decimal strings; signed ticks, quantums and nanosecond
timestamps are Python integers. Order writes are never automatically retried.
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


BASE_URLS = {
    "mainnet": "https://api.arcus.xyz",
    "testnet": "https://api.testnet.arcus.xyz",
}
SIDE = {"buy": 0, "sell": 1}
TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}
PENDING_STATUSES = {"ACK", "PENDING", "CANCEL_ACKNOWLEDGED", "CANCEL_PENDING"}
FAILED_STATUSES = {"REJECTED", "ERROR"}
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
TIMEFRAME_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
                     "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800, "12h": 43200,
                     "1d": 86400, "3d": 259200, "1w": 604800}


class ArcusError(RuntimeError):
    """Sanitized upstream error. uncertain=True means a write may have arrived."""

    def __init__(self, message: str, *, status_code: int = 0, code: str = "",
                 retry_after_ms: int = 0, uncertain: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after_ms = retry_after_ms
        self.uncertain = uncertain


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def decimal_value(value: Any, field: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ArcusError(f"{field} must be a decimal number", code="INVALID_DECIMAL") from exc
    if not result.is_finite():
        raise ArcusError(f"{field} must be finite", code="INVALID_DECIMAL")
    return result


def decimal_string(value: Decimal | str | int) -> str:
    result = format(decimal_value(value), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def to_units(value: Any, unit: Any) -> int:
    """Exact decimal-to-integer conversion, without binary floating point."""
    denominator = decimal_value(unit, "market increment")
    if denominator <= 0:
        raise ArcusError("Market increment must be positive", code="INVALID_MARKET")
    numerator_value = decimal_value(value)
    value_num, value_den = numerator_value.as_integer_ratio()
    unit_num, unit_den = denominator.as_integer_ratio()
    quotient, remainder = divmod(value_num * unit_den, value_den * unit_num)
    if remainder:
        raise ArcusError(f"{value} is not a multiple of {unit}", code="INVALID_INCREMENT")
    return quotient


def price_tick(market: dict[str, Any], price: Any) -> Decimal:
    """Placement tick for the price band. Signature always uses top-level tickSize."""
    value = decimal_value(price, "price")
    for tier in market.get("tickTiers") or []:
        if tier.get("upToPrice") is None or value < decimal_value(tier["upToPrice"]):
            return decimal_value(tier["tick"], "tier tick")
    return decimal_value(market["tickSize"], "tickSize")


def market_margin_fraction(market: dict[str, Any]) -> Decimal | None:
    """Current market floor; unknown RWA session state must not imply in-session."""
    base = market.get("initialMarginFraction")
    if base is None:
        return None
    base_fraction = decimal_value(base, "initialMarginFraction")
    if base_fraction <= 0:
        return None
    if market.get("isOutsideRth") is True:
        off_hours = market.get("offHoursInitialMarginFraction")
        if off_hours is None:
            return None
        off_fraction = decimal_value(off_hours, "offHoursInitialMarginFraction")
        return max(base_fraction, off_fraction) if off_fraction > 0 else None
    if market.get("isOutsideRth") is False or market.get("regularTradingHours") is None:
        return base_fraction
    return None


def effective_initial_margin_fraction(market: dict[str, Any], leverage: Any) -> Decimal | None:
    """Account opening requirement: max(1 / override, active market floor)."""
    active = market_margin_fraction(market)
    override = decimal_value(leverage, "leverage")
    if override <= 0:
        raise ArcusError("leverage must be positive", code="INVALID_LEVERAGE")
    return max(Decimal(1) / override, active) if active is not None else None


def typed_place_payload(address: str, account_index: int, market: dict[str, Any],
                        side: str, price: str, size: str, client_id: str,
                        reduce_only: bool, timestamp_ns: int, good_til_us: int,
                        time_in_force: str = "ALO") -> dict[str, Any]:
    payload = {"ad": address.lower(), "ai": account_index, "ct": timestamp_ns,
               "g": good_til_us * 1000, "m": int(market["marketId"]), "op": 1,
               "p": to_units(price, market["tickSize"]),
               "q": to_units(size, market["stepSize"]), "r": int(reduce_only),
               "s": SIDE[side.lower()], "t": TIF[time_in_force], "v": 1}
    if client_id:
        payload["c"] = client_id
    return payload


def typed_cancel_payload(address: str, account_index: int, market_id: int,
                         timestamp_ns: int, *, client_id: str = "",
                         order_id: str = "") -> dict[str, Any]:
    if bool(client_id) == bool(order_id):
        raise ArcusError("Cancellation requires exactly one order ID or client ID",
                         code="INVALID_REQUEST")
    payload = {"ad": address.lower(), "ai": account_index, "ct": timestamp_ns,
               "m": market_id, "op": 2, "v": 1}
    payload["id" if order_id else "c"] = order_id or client_id
    return payload


def normalize_order(raw: dict[str, Any], *, http_status: int = 200,
                    client_id: str = "", order_id: str = "") -> dict[str, Any]:
    status = str(raw.get("status") or "UNKNOWN").upper()
    size = str(raw.get("originalSize", raw.get("quantity", "0")))
    filled = raw.get("filledSize")
    if (filled is None and raw.get("remainingSize") is not None
            and (raw.get("originalSize") is not None or raw.get("quantity") is not None)):
        filled = decimal_string(decimal_value(size) - decimal_value(raw["remainingSize"]))
    # A 202 ACK carries no fill evidence; missing orders are never inferred filled.
    return {**raw, "status": status, "filledSize": str(filled) if filled is not None else None, "size": size,
            "side": str(raw.get("side") or raw.get("orderSide") or ""),
            "market": str(raw.get("marketDisplayName") or raw.get("symbol") or ""),
            "symbol": str(raw.get("marketDisplayName") or raw.get("symbol") or ""),
            "orderId": str(raw.get("orderId") or order_id),
            "clientId": str(raw.get("clientId") or client_id),
            "price": str(raw.get("price") or "0"),
            "accepted": http_status in (200, 202) and status not in FAILED_STATUSES | {"UNKNOWN"},
            "pending": status not in FAILED_STATUSES and (http_status == 202 or status in PENDING_STATUSES),
            "error": raw.get("rejectionReason") or raw.get("error") or "",
            "httpStatus": http_status}


class ArcusClient:
    def __init__(self, credentials: dict[str, Any] | None = None, *,
                 transport: httpx.BaseTransport | None = None):
        self.credentials = dict(credentials or {})
        self.environment = str(self.credentials.get("environment", "mainnet"))
        if self.environment not in BASE_URLS:
            raise ArcusError("environment must be mainnet or testnet", code="INVALID_ENVIRONMENT")
        self.api_key = str(self.credentials.get("apiKey", "")).removeprefix("0x").lower().strip()
        self.address = str(self.credentials.get("address", "")).lower().strip()
        self.account_index = int(self.credentials.get("accountIndex", 0))
        if not 0 <= self.account_index <= 9:
            raise ArcusError("accountIndex must be between 0 and 9", code="INVALID_ACCOUNT")
        self._private_key: Ed25519PrivateKey | None = None
        signing_key = str(self.credentials.get("signingKey", self.credentials.get("apiSigningKey", "")))
        if signing_key:
            try:
                secret = bytes.fromhex(signing_key.removeprefix("0x").strip())
                self._private_key = Ed25519PrivateKey.from_private_bytes(secret)
            except (ValueError, TypeError) as exc:
                raise ArcusError("API Signing Key must be a 32-byte hex Ed25519 private key",
                                 code="INVALID_SIGNING_KEY") from exc
            public = self._private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
            if not hmac.compare_digest(public, self.api_key):
                raise ArcusError("API Key does not match API Signing Key", code="KEY_MISMATCH")
        self.http = httpx.Client(base_url=BASE_URLS[self.environment], timeout=10,
                                 transport=transport, follow_redirects=False)
        self._order_ids: dict[str, str] = {}
        self._lock = threading.Lock()
        self._last_timestamp = 0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "ArcusClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _timestamp(self) -> int:
        with self._lock:
            timestamp = max(time.time_ns(), self._last_timestamp + 1)
            self._last_timestamp = timestamp
            return timestamp

    def _account_params(self) -> dict[str, Any]:
        if not re.fullmatch(r"0x[0-9a-f]{40}", self.address):
            raise ArcusError("A valid master Ethereum wallet address is required", code="INVALID_ADDRESS")
        return {"address": self.address, "accountIndex": self.account_index}

    def _require_signing_key(self) -> Ed25519PrivateKey:
        self._account_params()
        if not self._private_key:
            raise ArcusError("API Signing Key is required for trading", code="SIGNING_KEY_REQUIRED")
        return self._private_key

    def _headers(self, message: bytes, timestamp: int) -> dict[str, str]:
        signature = self._require_signing_key().sign(message).hex()
        return {"X-API-Key": self.api_key, "X-Timestamp": str(timestamp),
                "X-Signature": signature, "Content-Type": "application/json"}

    def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
              ) -> tuple[dict[str, Any], int]:
        try:
            response = self.http.request(method, path, params=params,
                                         content=canonical_json(body) if body is not None else None,
                                         headers=headers)
        except httpx.HTTPError as exc:
            # Never print the request or headers: they contain credentials.
            raise ArcusError("Arcus network request failed", code="NETWORK_ERROR",
                             uncertain=method != "GET") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ArcusError("Arcus returned a non-JSON response", status_code=response.status_code,
                             code="INVALID_RESPONSE", uncertain=method != "GET") from exc
        if not isinstance(data, dict):
            raise ArcusError("Arcus returned an unexpected response shape", status_code=response.status_code,
                             code="INVALID_RESPONSE", uncertain=method != "GET")
        if response.status_code >= 400:
            message = str(data.get("error") or data.get("rejectReason") or f"Arcus request failed ({response.status_code})")
            # Public API errors should not echo API secrets. Sanitize defensively.
            for secret in (self.api_key, str(self.credentials.get("signingKey", "")),
                           str(self.credentials.get("apiSigningKey", ""))):
                if secret:
                    message = message.replace(secret, "[redacted]")
            retry_ms = int(data.get("retryAfterMs") or 0)
            if not retry_ms and response.status_code == 429:
                try:
                    retry_ms = int(float(response.headers.get("Retry-After", "1")) * 1000)
                except ValueError:
                    retry_ms = 1000
            raise ArcusError(message[:400], status_code=response.status_code,
                             code=str(data.get("code") or data.get("errorType") or data.get("rejectReason") or data.get("reason") or ""),
                             retry_after_ms=retry_ms,
                             uncertain=method != "GET" and response.status_code >= 500)
        return data, response.status_code

    def markets(self) -> list[dict[str, Any]]:
        data, _ = self._call("GET", "/v1/markets")
        observed_at = time.time_ns() // 1_000_000
        result = []
        for market in data.get("markets", []):
            fraction = market_margin_fraction(market)
            base_fraction = decimal_value(market["initialMarginFraction"]) if market.get("initialMarginFraction") else None
            result.append({**market, "symbol": market["marketDisplayName"],
                           "price": str(market.get("markPrice") or "0"),
                           "lastPrice": str(market.get("markPrice") or "0"),
                           "change24h": str(market.get("priceChange24h") or "0"),
                           "observedAt": observed_at, "updatedAtMs": observed_at,
                           "effectiveInitialMarginFraction": decimal_string(fraction) if fraction is not None else None,
                           "marginStateKnown": fraction is not None,
                           "maxLeverage": int(Decimal(1) / base_fraction) if base_fraction and base_fraction > 0 else 0,
                           "effectiveMaxLeverage": int(Decimal(1) / fraction) if fraction is not None else None,
                           "baseMaxLeverage": int(Decimal(1) / base_fraction) if base_fraction and base_fraction > 0 else 0})
        return result

    def validate_key(self) -> dict[str, Any]:
        self._require_signing_key()
        data, _ = self._call("GET", "/v1/apiKeys", params={"address": self.address})
        matched = next((entry for entry in data.get("apiKeys", [])
                        if str(entry.get("apiKey", "")).lower() == self.api_key), None)
        if not matched or matched.get("status") != "ACTIVE":
            raise ArcusError("API Key is not active for this wallet address", code="KEY_NOT_ACTIVE")
        expiry = int(matched.get("validUntil") or 0)
        if expiry and expiry <= time.time_ns() // 1_000_000:
            raise ArcusError("API Key has expired", code="KEY_EXPIRED")
        if not matched.get("allSubaccounts", False) and int(matched.get("accountIndex", 0)) != self.account_index:
            raise ArcusError("API Key is not authorized for this subaccount", code="SUBACCOUNT_MISMATCH")
        return {"valid": True, "environment": self.environment, "address": self.address,
                "accountIndex": self.account_index, "validUntil": expiry,
                "apiWalletName": matched.get("apiWalletName", ""),
                "allSubaccounts": bool(matched.get("allSubaccounts", False)),
                "permissions": matched.get("permissions", [])}

    def open_orders(self, market: dict[str, Any] | str | None = None) -> list[dict[str, Any]]:
        params = self._account_params()
        params["limit"] = 1000
        if market:
            params["market"] = market["marketId"] if isinstance(market, dict) else market
        data, _ = self._call("GET", "/v1/openOrders", params=params)
        return [normalize_order(order) for order in data.get("orders", [])]

    def account(self) -> dict[str, Any]:
        try:
            raw, _ = self._call("GET", "/v1/account", params=self._account_params())
        except ArcusError as exc:
            if exc.status_code != 404 or "no activity" not in str(exc).lower():
                raise
            return {"equity": "0", "availableBalance": "0", "freeCollateral": "0",
                    "unrealizedPnl": "0", "balance": "0", "netQuoteBalance": "0",
                    "positions": [], "orders": [], "funded": False,
                    "address": self.address, "accountIndex": self.account_index}
        source = raw.get("positions", {})
        required = {"equity", "freeCollateral", "netQuoteBalance", "positions"}
        if not required.issubset(raw) or any(raw.get(key) is None for key in required):
            raise ArcusError("Arcus account response is missing required fields", code="INVALID_RESPONSE")
        positions = list(source.values()) if isinstance(source, dict) else list(source)
        normalized_positions = []
        unrealized = Decimal(0)
        for position in positions:
            normalized_positions.append({**position, "symbol": position.get("marketDisplayName", ""),
                                         "market": position.get("marketDisplayName", ""),
                                         "entryPrice": position.get("averageEntryPrice", "0"),
                                         "markPrice": position.get("markPx", "0")})
            unrealized += decimal_value(position.get("unrealizedPnl") or "0")
        return {**raw, "equity": str(raw.get("equity") or "0"),
                "availableBalance": str(raw.get("freeCollateral") or "0"),
                "balance": str(raw.get("netQuoteBalance") or "0"),
                "unrealizedPnl": decimal_string(unrealized), "positions": normalized_positions,
                "orders": self.open_orders(), "funded": True}

    def account_snapshot(self) -> dict[str, Any]:
        snapshot = self.account()
        return {**snapshot, "connected": True, "openOrders": snapshot["orders"],
                "updatedAtMs": time.time_ns() // 1_000_000}

    def candles(self, symbol: str, timeframe: str = "15m", count: int = 120) -> list[dict[str, Any]]:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ArcusError("Unsupported Arcus candle timeframe", code="INVALID_TIMEFRAME")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ArcusError("Candle count must be a positive integer", code="INVALID_CANDLE_COUNT")
        data, _ = self._call("GET", "/v1/candles", params={"market": symbol, "timeframe": timeframe,
                              "to": time.time_ns() // 1000, "countback": max(1, min(count, 1500))})
        bars = data.get("candles")
        if not isinstance(bars, list):
            raise ArcusError("Arcus response is missing the candles array", code="INVALID_RESPONSE")
        normalized = []
        duration_us = TIMEFRAME_SECONDS[timeframe] * 1_000_000
        for bar in bars:
            if not isinstance(bar, dict) or not isinstance(bar.get("isFinal"), bool):
                raise ArcusError("Arcus candle is missing authoritative boolean isFinal", code="INVALID_RESPONSE")
            try:
                open_us = int(bar["openTime"])
            except (KeyError, ValueError, TypeError) as exc:
                raise ArcusError("Arcus candle openTime is invalid", code="INVALID_RESPONSE") from exc
            if open_us < 100_000_000_000_000 or bar.get("timeframe", timeframe) != timeframe:
                raise ArcusError("Arcus candle timestamp/timeframe is invalid", code="INVALID_RESPONSE")
            normalized.append({**bar, "openTime": open_us, "openTimeUs": open_us,
                               "closeTimeUs": open_us + duration_us,
                               "time": open_us // 1_000_000,
                               "timeframeSeconds": TIMEFRAME_SECONDS[timeframe]})
        return sorted(normalized, key=lambda bar: bar["openTime"])

    def trend_candles(self, market: dict[str, Any] | str, timeframe: str = "30m",
                      count: int = 240) -> list[dict[str, Any]]:
        """Only exchange-confirmed completed OHLCV bars, without synthetic gap fills.

        REST countback sometimes includes the open bar; request one extra and
        require literal isFinal=true. Freshness policy belongs to the strategy,
        which can compare closeTimeUs with its clock and timeframeSeconds.
        """
        symbol = (market.get("marketDisplayName") or market.get("symbol")) if isinstance(market, dict) else market
        if not symbol:
            raise ArcusError("Candle market display name is required", code="INVALID_MARKET")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ArcusError("Candle count must be a positive integer", code="INVALID_CANDLE_COUNT")
        bars = self.candles(str(symbol), timeframe, min(count + 1, 1500))
        completed = [bar for bar in bars if bar["isFinal"] is True]
        seen = set()
        now_us = time.time_ns() // 1000
        for bar in completed:
            if bar["openTime"] in seen or bar["closeTimeUs"] > now_us:
                raise ArcusError("Arcus returned duplicate or future finalized bars", code="INVALID_RESPONSE")
            seen.add(bar["openTime"])
            prices = {key: decimal_value(bar.get(key), f"candle {key}") for key in ("open", "high", "low", "close")}
            if (min(prices.values()) <= 0 or prices["high"] < max(prices.values())
                    or prices["low"] > min(prices.values())):
                raise ArcusError("Arcus candle OHLC prices are inconsistent", code="INVALID_RESPONSE")
        return completed[-count:]

    def bbo(self, market: dict[str, Any] | str) -> dict[str, Any]:
        # Unlike markets/orders filters, the BBO path only accepts the display name.
        identifier = (market.get("marketDisplayName") or market.get("symbol")) if isinstance(market, dict) else market
        if not identifier:
            raise ArcusError("BBO requires a market display name", code="INVALID_MARKET")
        data, _ = self._call("GET", "/v1/bbo/" + quote(str(identifier), safe=""))
        return data

    def fee_tiers(self) -> list[dict[str, Any]]:
        data, _ = self._call("GET", "/v1/feetiers")
        return [{**tier, "makerRate": decimal_string(Decimal(tier["maker_fee_ppm"]) / 1_000_000),
                 "takerRate": decimal_string(Decimal(tier["taker_fee_ppm"]) / 1_000_000)}
                for tier in data.get("tiers", [])]

    def _validate_order(self, market: dict[str, Any], side: str, price: str,
                        size: str, client_id: str, reduce_only: bool) -> None:
        self._require_signing_key()
        if side.lower() not in SIDE:
            raise ArcusError("side must be buy or sell", code="INVALID_SIDE")
        if not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise ArcusError("client ID must contain 1–36 ASCII letters, digits, hyphens or underscores",
                             code="INVALID_CLIENT_ID")
        if market.get("status") != "ONLINE":
            raise ArcusError("Market is not online", code="MARKET_OFFLINE")
        quantity, limit = decimal_value(size, "size"), decimal_value(price, "price")
        if quantity <= 0 or limit <= 0:
            raise ArcusError("size and price must be positive", code="INVALID_ORDER")
        to_units(price, market["tickSize"])
        to_units(price, price_tick(market, price))
        to_units(size, market["stepSize"])
        if quantity < decimal_value(market.get("minOrderSize") or "0"):
            raise ArcusError("size is below market minimum", code="MIN_ORDER_SIZE")
        if market.get("maxOrderSize") and quantity > decimal_value(market["maxOrderSize"]):
            raise ArcusError("size exceeds market maximum", code="MAX_ORDER_SIZE")
        if not reduce_only and quantity * limit < decimal_value(market.get("minOrderNotional") or "0"):
            raise ArcusError("Order notional is below market minimum", code="MIN_ORDER_NOTIONAL")

    def place_order(self, market: dict[str, Any], side: str, price: str, size: str,
                    client_id: str, reduce_only: bool = False) -> dict[str, Any]:
        self._validate_order(market, side, price, size, client_id, reduce_only)
        timestamp = self._timestamp()
        # Official API mandates >= one month even for IOC/FOK. Forty days adds slack.
        good_til = timestamp // 1000 + 40 * 86_400 * 1_000_000
        payload = typed_place_payload(self.address, self.account_index, market, side,
                                      price, size, client_id, reduce_only, timestamp, good_til)
        body = {**self._account_params(), "marketId": int(market["marketId"]),
                "orderSide": side.upper(), "orderType": "LIMIT", "quantity": size,
                "price": price, "timeInForce": "ALO", "goodTilTime": str(good_til),
                "clientId": client_id, "reduceOnly": reduce_only, "timestamp": timestamp}
        raw, status = self._call("POST", "/v1/placeOrder", body=body,
                                 headers=self._headers(canonical_json(payload), timestamp))
        normalized = normalize_order(raw, http_status=status, client_id=client_id)
        # Gateway ACK must contain the server ID. Uncertain writes cannot be resubmitted blindly.
        if not normalized["orderId"] and normalized["accepted"]:
            raise ArcusError("Arcus acknowledged an order without an order ID", code="MISSING_ORDER_ID", uncertain=True)
        if normalized["orderId"]:
            with self._lock:
                self._order_ids[client_id] = normalized["orderId"]
        normalized["size"] = size
        normalized["price"] = price
        return normalized

    def _remember_order_result(self, raw: dict[str, Any], http_status: int,
                               client_id: str, price: str, size: str) -> dict[str, Any]:
        normalized = normalize_order(raw, http_status=http_status, client_id=client_id)
        if normalized["accepted"] and not normalized["orderId"]:
            raise ArcusError("Arcus acknowledged an order without an order ID", code="MISSING_ORDER_ID", uncertain=True)
        if normalized["orderId"]:
            with self._lock:
                self._order_ids[client_id] = normalized["orderId"]
        if raw.get("originalSize") is None and raw.get("quantity") is None:
            normalized["size"] = size
        if raw.get("price") is None:
            normalized["price"] = price
        return normalized

    def place_stop_loss(self, market: dict[str, Any], side: str, stop_price: str,
                        limit_price: str, client_id: str) -> dict[str, Any]:
        """Native full-position STOP_LOSS LIMIT/GTT; it can take at trigger time.

        The one-leg positionTpsl grouping uses quantity=0 by specification. It
        must only be used when this strategy owns the market's entire position.
        A limit stop does not guarantee a fill across a gap.
        """
        self._require_signing_key()
        if side.lower() not in SIDE or not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise ArcusError("Invalid stop side or client ID", code="INVALID_REQUEST")
        if market.get("status") != "ONLINE":
            raise ArcusError("Market is not online", code="MARKET_OFFLINE")
        trigger = decimal_value(stop_price, "stop price")
        limit = decimal_value(limit_price, "stop limit price")
        if trigger <= 0 or limit <= 0:
            raise ArcusError("Stop and limit prices must be positive", code="INVALID_STOP_PRICE")
        for value in (stop_price, limit_price):
            to_units(value, market["tickSize"])
            to_units(value, price_tick(market, value))
        oracle = decimal_value(market.get("oraclePrice"), "oracle price")
        if oracle <= 0:
            raise ArcusError("Current oracle price is required for native stop validation", code="INVALID_MARKET")
        if (side.lower() == "sell" and trigger >= oracle) or (side.lower() == "buy" and trigger <= oracle):
            raise ArcusError("Stop trigger has already been reached", code="STOP_TRIGGER_ALREADY_REACHED")
        timestamp = self._timestamp()
        good_til = timestamp // 1000 + 40 * 86_400 * 1_000_000
        payload = typed_place_payload(self.address, self.account_index, market, side,
                                      limit_price, "0", client_id, True, timestamp, good_til, "GTT")
        payload["op"] = 4
        signature = self._require_signing_key().sign(canonical_json(payload)).hex()
        order = {**self._account_params(), "marketId": int(market["marketId"]),
                 "orderSide": side.upper(), "orderType": "LIMIT", "quantity": "0",
                 "price": limit_price, "timeInForce": "GTT", "goodTilTime": str(good_til),
                 "clientId": client_id, "reduceOnly": True, "tpslType": "STOP_LOSS",
                 "stopPrice": stop_price, "isPositionTPSL": True,
                 "timestamp": timestamp, "signature": signature}
        raw, status = self._call("POST", "/v1/batchPlaceOrders", params=self._account_params(),
                                 body={"grouping": "positionTpsl", "orders": [order]},
                                 headers={"X-API-Key": self.api_key, "X-Timestamp": str(timestamp),
                                          "X-Signature": signature, "Content-Type": "application/json"})
        responses = raw.get("responses")
        if not isinstance(responses, list) or len(responses) != 1 or not isinstance(responses[0], dict):
            raise ArcusError("Arcus stop batch result is not correlated", code="INVALID_RESPONSE", uncertain=True)
        return self._remember_order_result(responses[0], status, client_id, limit_price, "0")

    def emergency_reduce(self, market: dict[str, Any], side: str, size: str,
                         client_id: str, slippage: str = "0.02") -> dict[str, Any]:
        """One bounded MARKET/IOC reduce-only write; it may fill partially or not at all.

        This is an explicit risk-exit exception to ordinary ALO placement. Never
        retry an uncertain write; reconcile its durable client/server IDs first.
        """
        if side.lower() not in SIDE:
            raise ArcusError("side must be buy or sell", code="INVALID_SIDE")
        mark = decimal_value(market.get("markPrice"), "mark price")
        tolerance = decimal_value(slippage, "slippage")
        if mark <= 0 or tolerance <= 0 or tolerance > Decimal("0.1"):
            raise ArcusError("Emergency exit needs a positive mark and slippage in (0, 0.1]",
                             code="INVALID_SLIPPAGE_BOUND")
        bound = mark * (1 + tolerance if side.lower() == "buy" else 1 - tolerance)
        rounding = ROUND_FLOOR if side.lower() == "buy" else ROUND_CEILING
        candidate = bound
        for _ in range(16):
            tick = price_tick(market, candidate)
            candidate = (bound / tick).to_integral_value(rounding=rounding) * tick
            try:
                to_units(candidate, price_tick(market, candidate))
                break
            except ArcusError:
                continue
        else:
            raise ArcusError("Emergency slippage bound cannot align to market ticks", code="INVALID_INCREMENT")
        if candidate <= 0 or abs(candidate - mark) / mark > Decimal("0.1"):
            raise ArcusError("Emergency price bound violates exchange maximum", code="INVALID_SLIPPAGE_BOUND")
        price = decimal_string(candidate)
        self._validate_order(market, side, price, size, client_id, True)
        timestamp = self._timestamp()
        good_til = timestamp // 1000 + 40 * 86_400 * 1_000_000
        payload = typed_place_payload(self.address, self.account_index, market, side,
                                      price, size, client_id, True, timestamp, good_til, "IOC")
        body = {**self._account_params(), "marketId": int(market["marketId"]),
                "orderSide": side.upper(), "orderType": "MARKET", "quantity": size,
                "price": price, "timeInForce": "IOC", "goodTilTime": str(good_til),
                "clientId": client_id, "reduceOnly": True, "timestamp": timestamp}
        raw, status = self._call("POST", "/v1/placeOrder", body=body,
                                 headers=self._headers(canonical_json(payload), timestamp))
        return self._remember_order_result(raw, status, client_id, price, size)

    def stop_status(self, market: dict[str, Any], client_id: str,
                    order_id: str | None = None) -> dict[str, Any]:
        """Preserve the official unified lifecycle; no undocumented child mapping.

        parentOrderId is an entry-TPSL binding, not a generated execution child.
        Arcus does not document childOrderId or a historical triggeredAt field.
        A trigger event's updatedAt supplies its event time only while that event
        is the current status. Cancellation is not evidence of having triggered.
        """
        result = self.order_status(market, client_id, order_id)
        status = result["status"]
        filled = result.get("filledSize")
        if status in ("UNTRIGGERED", "TPSL_PLACED"):
            triggered = False
        elif status == "TPSL_TRIGGERED" or (filled is not None and decimal_value(filled) > 0):
            triggered = True
        else:
            triggered = None
        return {**result, "triggered": triggered,
                "triggeredAt": result.get("updatedAt") if status == "TPSL_TRIGGERED" else None,
                "childOrderId": None}

    def cancel_stop_loss(self, market: dict[str, Any], client_id: str,
                         order_id: str | None = None) -> dict[str, Any]:
        """Cancel only the correlated stop ID; its ACK still requires reconciliation."""
        return self.cancel_order(market, client_id, order_id)

    def cancel_order(self, market: dict[str, Any], client_id: str,
                     order_id: str | None = None) -> dict[str, Any]:
        timestamp = self._timestamp()
        payload = typed_cancel_payload(self.address, self.account_index, int(market["marketId"]),
                                       timestamp, client_id="" if order_id else client_id,
                                       order_id=order_id or "")
        body = {**self._account_params(), "marketId": int(market["marketId"]),
                "kind": "orderId" if order_id else "clientId", "timestamp": timestamp}
        body["orderId" if order_id else "clientId"] = order_id or client_id
        raw, status = self._call("POST", "/v1/cancelOrder", body=body,
                                 headers=self._headers(canonical_json(payload), timestamp))
        return normalize_order(raw, http_status=status, client_id=client_id, order_id=order_id or "")

    def order_status(self, market: dict[str, Any], client_id: str,
                     order_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            resolved_id = order_id or self._order_ids.get(client_id)
        if resolved_id:
            try:
                raw, status = self._call("GET", "/v1/order/" + quote(resolved_id, safe=""),
                                         params=self._account_params())
                return normalize_order(raw, http_status=status, client_id=client_id, order_id=resolved_id)
            except ArcusError as exc:
                if exc.status_code != 404:
                    raise
                # ACK may not yet have reached the account datastore.
        params = {**self._account_params(), "market": market["marketId"], "limit": 1000}
        raw, _ = self._call("GET", "/v1/orders", params=params)
        match = next((order for order in raw.get("orders", [])
                      if (resolved_id and order.get("orderId") == resolved_id)
                      or (not resolved_id and order.get("clientId") == client_id)), None)
        if match:
            return normalize_order(match, client_id=client_id, order_id=resolved_id or "")
        return normalize_order({"status": "UNKNOWN", "error": "Order not visible in account history yet"},
                               client_id=client_id, order_id=resolved_id or "")

    def fills(self, market: dict[str, Any] | str | None = None, limit: int = 100,
              from_us: int | None = None, to_us: int | None = None) -> list[dict[str, Any]]:
        params = {**self._account_params(), "limit": max(1, min(limit, 1000))}
        if market:
            params["market"] = market["marketId"] if isinstance(market, dict) else market
        if from_us is not None:
            params["from"] = from_us
        if to_us is not None:
            params["to"] = to_us
        raw, _ = self._call("GET", "/v1/fills", params=params)
        rows = raw.get("fills")
        if not isinstance(rows, list):
            raise ArcusError("Arcus response is missing the fills array", code="INVALID_RESPONSE")
        for row in rows:
            if not isinstance(row, dict) or any(row.get(field) is None for field in
                    ("tradeId", "orderId", "size", "price", "fee", "createdAt")):
                raise ArcusError("Arcus fill ledger is missing required fields", code="INVALID_RESPONSE")
            for field in ("size", "price", "fee"):
                decimal_value(row[field], f"fill {field}")
        return rows

    def funding_payments(self, market: dict[str, Any] | str | None = None,
                         from_us: int | None = None, to_us: int | None = None,
                         limit: int = 1000) -> list[dict[str, Any]]:
        """Persisted funding ledger, available after a position has closed.

        payment is quote currency: positive received, negative paid. time and
        inclusive range bounds are microseconds. A full page needs further
        paging; total is only page length, not proof of a complete range.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ArcusError("Funding limit must be a positive integer", code="INVALID_REQUEST")
        params = {**self._account_params(), "limit": min(limit, 1000)}
        if market:
            params["market"] = market["marketId"] if isinstance(market, dict) else market
        for key, value in (("from", from_us), ("to", to_us)):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 100_000_000_000_000:
                    raise ArcusError("Funding range bounds must be epoch microseconds", code="INVALID_TIMESTAMP")
                params[key] = value
        if from_us is not None and to_us is not None and from_us > to_us:
            raise ArcusError("Funding from must not exceed to", code="INVALID_TIMESTAMP")
        raw, _ = self._call("GET", "/v1/funding", params=params)
        rows = raw.get("fundingPayments")
        if not isinstance(rows, list):
            raise ArcusError("Arcus response is missing the funding payment ledger", code="INVALID_RESPONSE")
        for row in rows:
            if not isinstance(row, dict) or any(row.get(field) is None for field in ("marketId", "payment", "time")):
                raise ArcusError("Arcus funding payment is missing required fields", code="INVALID_RESPONSE")
            decimal_value(row["payment"], "funding payment")
            if (isinstance(row["time"], bool) or not isinstance(row["time"], int)
                    or row["time"] < 100_000_000_000_000):
                raise ArcusError("Arcus funding payment time is not epoch microseconds", code="INVALID_RESPONSE")
        return rows

    def set_leverage(self, market: dict[str, Any], leverage: int,
                     isolated: bool | None = None) -> dict[str, Any]:
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 1000:
            raise ArcusError("leverage must be an integer between 1 and 1000", code="INVALID_LEVERAGE")
        base_fraction = decimal_value(market.get("initialMarginFraction"), "initialMarginFraction")
        if base_fraction <= 0 or Decimal(leverage) * base_fraction > 1:
            raise ArcusError("leverage exceeds this market's maximum", code="INVALID_LEVERAGE")
        if isolated is not None and not isinstance(isolated, bool):
            raise ArcusError("isolated must be a boolean", code="INVALID_MARGIN_MODE")
        body = {**self._account_params(), "marketId": int(market["marketId"]), "leverage": leverage}
        if isolated is not None:
            body["isolated"] = isolated
        return self._legacy_write("setLeverage", body)

    def margin_config(self, market: dict[str, Any]) -> dict[str, Any]:
        """Authoritative per-market config, available even without an open position."""
        data, _ = self._call("GET", "/v1/leverages", params=self._account_params())
        entry = next((row for row in data.get("leverages", [])
                      if str(row.get("marketId")) == str(market["marketId"])), None)
        if entry is None:
            raise ArcusError("Arcus leverage snapshot does not include this market", code="MARGIN_CONFIG_MISSING")
        leverage = decimal_value(entry.get("leverage"), "effective leverage")
        mode = str(entry.get("marginMode") or "").upper()
        if leverage < 1 or leverage != leverage.to_integral_value() or mode not in ("CROSS", "ISOLATED"):
            raise ArcusError("Arcus returned invalid leverage/margin configuration", code="INVALID_RESPONSE")
        if entry.get("isolated") is not None and entry["isolated"] != (mode == "ISOLATED"):
            raise ArcusError("Arcus margin-mode fields disagree", code="INVALID_RESPONSE")
        fraction = effective_initial_margin_fraction(market, leverage)
        return {**entry, "leverage": int(leverage), "marginMode": mode, "isolated": mode == "ISOLATED",
                "effectiveInitialMarginFraction": decimal_string(fraction) if fraction is not None else None,
                "verified": True, "observedAt": time.time_ns() // 1_000_000}

    def configure_margin(self, market: dict[str, Any], leverage: int,
                         marginMode: str) -> dict[str, Any]:
        """Apply requested config, then verify an authoritative GET snapshot.

        This method performs no order placement. ACK, an unavailable snapshot,
        or a snapshot still showing old config returns PENDING; callers must
        prohibit live orders until a later snapshot matches both requested fields.
        """
        self._require_signing_key()
        mode = str(marginMode).upper()
        if mode not in ("CROSS", "ISOLATED"):
            raise ArcusError("marginMode must be CROSS or ISOLATED", code="INVALID_MARGIN_MODE")
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 1000:
            raise ArcusError("leverage must be an integer between 1 and 1000", code="INVALID_LEVERAGE")
        base_fraction = decimal_value(market.get("initialMarginFraction"), "initialMarginFraction")
        if base_fraction <= 0 or Decimal(leverage) * base_fraction > 1:
            raise ArcusError("leverage exceeds this market's maximum", code="INVALID_LEVERAGE")
        before = self.margin_config(market)
        if before["leverage"] == leverage and before["marginMode"] == mode:
            return {**before, "status": "APPLIED", "pending": False, "verified": True,
                    "current": before, "requestedLeverage": leverage, "requestedMarginMode": mode,
                    "requestId": None, "changed": False}
        response = self.set_leverage(market, leverage, isolated=mode == "ISOLATED")
        response_status = str(response.get("status") or "UNKNOWN").upper()
        if response_status == "REJECTED":
            reason = str(response.get("rejectReason") or "MARGIN_CHANGE_REJECTED")
            raise ArcusError(reason, status_code=422, code=reason)
        verification_error = None
        try:
            current = self.margin_config(market)
        except ArcusError as exc:
            current = None
            verification_error = str(exc)
        verified = bool(current and current["leverage"] == leverage and current["marginMode"] == mode)
        return {"status": "APPLIED" if verified else "PENDING", "verified": verified,
                "pending": not verified, "requestId": response.get("requestId"), "changed": True,
                "requestedLeverage": leverage, "requestedMarginMode": mode, "current": current,
                "leverage": current["leverage"] if current else None,
                "marginMode": current["marginMode"] if current else None,
                "responseStatus": response_status, "verificationError": verification_error}

    def schedule_cancel(self, deadline_us: int | None) -> dict[str, Any]:
        """Account-wide dead man's switch; not activated by the panel's worker.

        This cancels every order in the subaccount, including externally placed
        orders. The panel cancels only its persisted order IDs instead.
        """
        body = self._account_params()
        if deadline_us is not None:
            body["time"] = deadline_us
        return self._legacy_write("scheduleCancel", body)

    def _legacy_write(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        timestamp = self._timestamp()
        message = str(timestamp).encode() + action.encode() + canonical_json(body)
        raw, status = self._call("POST", "/v1/" + action, body=body,
                                 headers=self._headers(message, timestamp))
        return {**raw, "httpStatus": status}
