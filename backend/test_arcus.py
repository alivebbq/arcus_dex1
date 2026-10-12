"""Deterministic, network-free tests of the official Arcus wire contract."""
import json
import unittest
from decimal import Decimal

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.arcus import (ArcusClient, ArcusError, canonical_json, normalize_order,
                          to_units, typed_cancel_payload, typed_place_payload,
                          effective_initial_margin_fraction)


# RFC 8032 public example key, never an account credential.
SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
MARKET = {"marketId": 1, "marketDisplayName": "BTC-USD", "status": "ONLINE",
          "tickSize": "0.1", "stepSize": "0.00000001", "minOrderSize": "0.0001",
          "maxOrderSize": "10000", "minOrderNotional": "5",
          "initialMarginFraction": "0.025", "offHoursInitialMarginFraction": "0.025",
          "regularTradingHours": None, "isOutsideRth": False,
          "markPrice": "50000.5", "oraclePrice": "50000.5",
          "tickTiers": [{"upToPrice": "500000", "tick": "0.1"}, {"tick": "0.2"}]}


class ArcusWireTests(unittest.TestCase):
    def client(self, handler):
        result = ArcusClient({"apiKey": PUBLIC, "signingKey": SEED, "address": ADDRESS},
                             transport=httpx.MockTransport(handler))
        self.addCleanup(result.close)
        return result

    def test_canonical_signature_payload_matches_official_field_order_and_units(self):
        payload = typed_place_payload(ADDRESS.upper().replace("0X", "0x"), 2, MARKET,
                                      "buy", "50000.5", "0.001", "MiXeD_1", False,
                                      1712345678000000000, 4102444800000000)
        expected = ('{"ad":"' + ADDRESS + '","ai":2,"c":"MiXeD_1",'
                    '"ct":1712345678000000000,"g":4102444800000000000,"m":1,"op":1,'
                    '"p":500005,"q":100000,"r":0,"s":0,"t":3,"v":1}')
        self.assertEqual(canonical_json(payload).decode(), expected)
        canceled = typed_cancel_payload(ADDRESS, 2, 1, 1712345678000000000,
                                        client_id="MiXeD_1")
        self.assertEqual(canonical_json(canceled).decode(),
                         '{"ad":"' + ADDRESS + '","ai":2,"c":"MiXeD_1",'
                         '"ct":1712345678000000000,"m":1,"op":2,"v":1}')

    def test_live_order_wire_is_alo_and_verifies_against_registered_public_key(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["timeInForce"], "ALO")
            self.assertEqual(body["orderType"], "LIMIT")
            self.assertEqual(body["quantity"], "0.001")
            timestamp = int(request.headers["X-Timestamp"])
            self.assertEqual(body["timestamp"], timestamp)
            self.assertGreater(int(body["goodTilTime"]) - timestamp // 1000,
                               31 * 86400 * 1_000_000)
            payload = typed_place_payload(ADDRESS, 0, MARKET, "buy", body["price"],
                                          body["quantity"], body["clientId"], False,
                                          timestamp, int(body["goodTilTime"]))
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC)).verify(
                bytes.fromhex(request.headers["X-Signature"]), canonical_json(payload))
            return httpx.Response(202, json={"orderId": "abcd", "status": "ACK"})
        response = self.client(handler).place_order(MARKET, "buy", "50000.5", "0.001", "MiXeD_1")
        self.assertEqual(response["status"], "ACK")
        self.assertTrue(response["pending"])
        self.assertIsNone(response["filledSize"])

    def test_signature_always_uses_base_tick_at_coarser_price_band(self):
        payload = typed_place_payload(ADDRESS, 0, MARKET, "sell", "500000.2", "0.001", "a",
                                      False, 1712345678000000000, 4102444800000000)
        self.assertEqual(payload["p"], 5000002)
        client = self.client(lambda req: self.fail("No request should be issued"))
        with self.assertRaises(ArcusError):
            client.place_order(MARKET, "sell", "500000.1", "0.001", "a")

    def test_nanosecond_and_tiny_decimal_math_does_not_round(self):
        self.assertEqual(to_units("0.00000003", "0.00000001"), 3)
        with self.assertRaises(ArcusError):
            to_units("1.00000000000000000000000000001", "0.1")

    def test_missing_and_cancel_ack_are_not_fills(self):
        client = self.client(lambda request: httpx.Response(200, json={"orders": []}))
        missing = client.order_status(MARKET, "not-yet-visible")
        self.assertEqual(missing["status"], "UNKNOWN")
        self.assertFalse(missing["accepted"])
        self.assertIsNone(missing["filledSize"])
        acknowledgment = normalize_order({"status": "CANCEL_ACKNOWLEDGED"}, http_status=202)
        self.assertTrue(acknowledgment["pending"])
        self.assertNotEqual(acknowledgment["status"], "CANCELED")

    def test_http200_engine_rejection_is_rejected(self):
        client = self.client(lambda request: httpx.Response(200, json={
            "orderId": "abc", "status": "REJECTED", "rejectionReason": "POST_ONLY_WOULD_CROSS"}))
        result = client.place_order(MARKET, "buy", "50000.5", "0.001", "a")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error"], "POST_ONLY_WOULD_CROSS")

    def test_uncertain_write_is_never_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("timed out")
        client = self.client(handler)
        with self.assertRaises(ArcusError) as raised:
            client.place_order(MARKET, "buy", "50000.5", "0.001", "a")
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(len(calls), 1)

    def test_key_mismatch_and_expired_or_wrong_subaccount_rejected(self):
        with self.assertRaises(ArcusError):
            ArcusClient({"apiKey": "00" * 32, "signingKey": SEED, "address": ADDRESS})
        for entry in [
            {"apiKey": PUBLIC, "status": "ACTIVE", "validUntil": 1},
            {"apiKey": PUBLIC, "status": "ACTIVE", "validUntil": 0, "accountIndex": 1},
        ]:
            client = self.client(lambda request: httpx.Response(200, json={"apiKeys": [entry]}))
            with self.assertRaises(ArcusError):
                client.validate_key()

    def test_status_uses_persisted_server_id_and_retains_authoritative_partial_fill(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/order/abcdef")
            return httpx.Response(200, json={"orderId": "abcdef", "status": "PARTIALLY_FILLED",
                "originalSize": "0.003", "remainingSize": "0.002", "avgFillPrice": "49999.8"})
        result = self.client(handler).order_status(MARKET, "a", order_id="abcdef")
        self.assertEqual(result["filledSize"], "0.001")
        self.assertEqual(result["avgFillPrice"], "49999.8")

    def test_flat_margin_config_reads_every_market_without_positions(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/leverages")
            self.assertEqual(request.url.params["address"], ADDRESS)
            self.assertEqual(request.url.params["accountIndex"], "0")
            return httpx.Response(200, json={"leverages": [{"marketId": 1,
                "marketDisplayName": "BTC-USD", "leverage": 5, "isolated": True,
                "marginMode": "ISOLATED"}]})
        result = self.client(handler).margin_config(MARKET)
        self.assertEqual(result["leverage"], 5)
        self.assertEqual(result["marginMode"], "ISOLATED")
        self.assertEqual(result["effectiveInitialMarginFraction"], "0.2")

    def test_configure_margin_verifies_ack_by_snapshot_and_only_writes_once(self):
        calls = []
        get_count = 0
        def handler(request):
            nonlocal get_count
            calls.append(request.method)
            if request.method == "GET":
                get_count += 1
                return httpx.Response(200, json={"leverages": [{"marketId": 1,
                    "leverage": 40 if get_count == 1 else 5,
                    "isolated": get_count != 1,
                    "marginMode": "CROSS" if get_count == 1 else "ISOLATED"}]})
            body = json.loads(request.content)
            self.assertEqual(body["leverage"], 5)
            self.assertIs(body["isolated"], True)
            message = request.headers["X-Timestamp"].encode() + b"setLeverage" + canonical_json(body)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC)).verify(
                bytes.fromhex(request.headers["X-Signature"]), message)
            return httpx.Response(202, json={"status": "ACK", "requestId": "cfg-1", "leverage": 5})
        result = self.client(handler).configure_margin(MARKET, 5, "ISOLATED")
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_ack_or_applied_without_matching_read_is_pending(self):
        for upstream_status in ("ACK", "APPLIED"):
            with self.subTest(upstream_status=upstream_status):
                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(200, json={"leverages": [{"marketId": 1,
                            "leverage": 40, "isolated": False, "marginMode": "CROSS"}]})
                    return httpx.Response(202 if upstream_status == "ACK" else 200,
                                         json={"status": upstream_status, "requestId": "cfg-1", "leverage": 5})
                result = self.client(handler).configure_margin(MARKET, 5, "ISOLATED")
                self.assertFalse(result["verified"])
                self.assertTrue(result["pending"])
                self.assertEqual(result["status"], "PENDING")
                self.assertEqual(result["current"]["leverage"], 40)

    def test_existing_matching_config_does_not_send_write(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={"leverages": [{"marketId": 1,
                "leverage": 5, "isolated": False, "marginMode": "CROSS"}]})
        result = self.client(handler).configure_margin(MARKET, 5, "CROSS")
        self.assertTrue(result["verified"])
        self.assertFalse(result["changed"])
        self.assertEqual(calls, ["GET"])

    def test_engine_reject_reason_is_preserved_and_not_counted_applied(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json={"leverages": [{"marketId": 1,
                    "leverage": 40, "isolated": False, "marginMode": "CROSS"}]})
            return httpx.Response(422, json={"status": "REJECTED", "rejectReason": "HAS_OPEN_POSITION",
                                             "leverage": 40})
        with self.assertRaises(ArcusError) as raised:
            self.client(handler).configure_margin(MARKET, 5, "ISOLATED")
        self.assertEqual(raised.exception.code, "HAS_OPEN_POSITION")
        self.assertEqual(calls, ["GET", "POST"])

    def test_off_hours_margin_floor_and_unknown_session_are_not_guessed(self):
        off_hours = {**MARKET, "initialMarginFraction": "0.1", "offHoursInitialMarginFraction": "0.15",
                     "regularTradingHours": {"timezone": "America/New_York"}, "isOutsideRth": True}
        self.assertEqual(effective_initial_margin_fraction(off_hours, 10), Decimal("0.15"))
        self.assertEqual(effective_initial_margin_fraction(off_hours, 5), Decimal("0.2"))
        client = self.client(lambda request: httpx.Response(200, json={"markets": [off_hours]}))
        result = client.markets()[0]
        self.assertEqual(result["effectiveInitialMarginFraction"], "0.15")
        self.assertEqual(result["maxLeverage"], 10)
        self.assertEqual(result["effectiveMaxLeverage"], 6)
        self.assertEqual(result["baseMaxLeverage"], 10)
        off_hours["isOutsideRth"] = None
        self.assertIsNone(effective_initial_margin_fraction(off_hours, 5))
        result = client.markets()[0]
        self.assertFalse(result["marginStateKnown"])
        self.assertEqual(result["maxLeverage"], 10)
        self.assertIsNone(result["effectiveMaxLeverage"])

    def test_post_applied_but_snapshot_unavailable_stays_pending(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            if request.method == "POST":
                return httpx.Response(200, json={"status": "APPLIED", "requestId": "cfg-1", "leverage": 5})
            if len(calls) == 1:
                return httpx.Response(200, json={"leverages": [{"marketId": 1,
                    "leverage": 40, "isolated": False, "marginMode": "CROSS"}]})
            raise httpx.ReadTimeout("snapshot unavailable")
        result = self.client(handler).configure_margin(MARKET, 5, "CROSS")
        self.assertEqual(result["status"], "PENDING")
        self.assertFalse(result["verified"])
        self.assertIsNone(result["current"])
        self.assertIsNotNone(result["verificationError"])
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_margin_timeout_is_uncertain_and_no_repeated_write(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json={"leverages": [{"marketId": 1,
                    "leverage": 40, "isolated": False, "marginMode": "CROSS"}]})
            raise httpx.ReadTimeout("timed out after send")
        with self.assertRaises(ArcusError) as raised:
            self.client(handler).configure_margin(MARKET, 5, "CROSS")
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(calls, ["GET", "POST"])

    def test_bad_leverage_or_mode_never_writes(self):
        client = self.client(lambda request: self.fail("Invalid config must not issue a request"))
        for leverage, mode in [(0, "CROSS"), (41, "CROSS"), (True, "CROSS"), (5, "wrong")]:
            with self.subTest(leverage=leverage, mode=mode), self.assertRaises(ArcusError):
                client.configure_margin(MARKET, leverage, mode)

    @staticmethod
    def candle(open_us, is_final=True, **changes):
        return {"marketDisplayName": "BTC-USD", "marketId": 1, "timeframe": "30m",
                "openTime": open_us, "open": "100", "high": "102", "low": "99",
                "close": "101", "isFinal": is_final, **changes}

    def test_trend_candles_excludes_open_bar_and_preserves_exact_time_units(self):
        bars = [self.candle(1712345400000000, False), self.candle(1712341800000000),
                self.candle(1712343600000000)]
        def handler(request):
            self.assertEqual(request.url.path, "/v1/candles")
            self.assertEqual(request.url.params["timeframe"], "30m")
            self.assertEqual(request.url.params["countback"], "3")
            self.assertGreater(int(request.url.params["to"]), 100000000000000)
            return httpx.Response(200, json={"candles": bars})
        result = self.client(handler).trend_candles(MARKET, "30m", 2)
        self.assertEqual([bar["openTime"] for bar in result], [1712341800000000, 1712343600000000])
        self.assertTrue(all(bar["isFinal"] is True for bar in result))
        self.assertEqual(result[0]["time"], 1712341800)
        self.assertEqual(result[0]["openTimeUs"], 1712341800000000)
        self.assertEqual(result[0]["closeTimeUs"], 1712343600000000)
        self.assertEqual(result[0]["timeframeSeconds"], 1800)

    def test_candle_finality_and_ohlc_are_not_inferred(self):
        for bad in [self.candle(1712341800000000, "true"),
                    self.candle(1712341800000000, True, high="98")]:
            with self.subTest(bad=bad):
                client = self.client(lambda request: httpx.Response(200, json={"candles": [bad]}))
                with self.assertRaises(ArcusError):
                    client.trend_candles(MARKET, "30m", 2)
        client = self.client(lambda request: httpx.Response(200, json={"candles": []}))
        self.assertEqual(client.trend_candles(MARKET), [])

    def test_unsupported_candle_timeframe_does_not_issue_request(self):
        client = self.client(lambda request: self.fail("Unsupported timeframe must not request"))
        with self.assertRaises(ArcusError):
            client.candles("BTC-USD", "10m")

    def test_native_stop_uses_signed_position_tpsl_op4_quantity_zero_gtt(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/batchPlaceOrders")
            body = json.loads(request.content)
            self.assertEqual(body["grouping"], "positionTpsl")
            self.assertEqual(len(body["orders"]), 1)
            order = body["orders"][0]
            self.assertEqual(order["quantity"], "0")
            self.assertEqual(order["stopPrice"], "49000")
            self.assertEqual(order["price"], "49000.2")
            self.assertEqual(order["tpslType"], "STOP_LOSS")
            self.assertTrue(order["isPositionTPSL"])
            self.assertTrue(order["reduceOnly"])
            self.assertEqual(order["timeInForce"], "GTT")
            timestamp = int(request.headers["X-Timestamp"])
            payload = typed_place_payload(ADDRESS, 0, MARKET, "sell", order["price"], "0",
                order["clientId"], True, timestamp, int(order["goodTilTime"]), "GTT")
            payload["op"] = 4
            self.assertEqual(payload["q"], 0)
            self.assertEqual(payload["t"], 0)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC)).verify(
                bytes.fromhex(order["signature"]), canonical_json(payload))
            self.assertEqual(request.headers["X-Signature"], order["signature"])
            return httpx.Response(202, json={"responses": [{"status": "ACK", "orderId": "stop-1"}]})
        result = self.client(handler).place_stop_loss(MARKET, "sell", "49000", "49000.2", "native-stop")
        self.assertEqual(result["orderId"], "stop-1")
        self.assertTrue(result["pending"])
        self.assertIsNone(result["filledSize"])

    def test_native_stop_validates_crossed_trigger_and_uncorrelated_response(self):
        client = self.client(lambda request: self.fail("Already reached trigger must not submit"))
        with self.assertRaises(ArcusError) as raised:
            client.place_stop_loss(MARKET, "sell", "50001", "50001.2", "native-stop")
        self.assertEqual(raised.exception.code, "STOP_TRIGGER_ALREADY_REACHED")
        client = self.client(lambda request: httpx.Response(202, json={"responses": []}))
        with self.assertRaises(ArcusError) as raised:
            client.place_stop_loss(MARKET, "sell", "49000", "49000.2", "native-stop")
        self.assertTrue(raised.exception.uncertain)

    def test_emergency_exit_is_reduce_only_market_ioc_with_bounded_price_and_partial_accounting(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["orderType"], "MARKET")
            self.assertEqual(body["timeInForce"], "IOC")
            self.assertIs(body["reduceOnly"], True)
            self.assertEqual(body["price"], "49000.5")
            timestamp = int(request.headers["X-Timestamp"])
            payload = typed_place_payload(ADDRESS, 0, MARKET, "sell", body["price"], "0.001",
                body["clientId"], True, timestamp, int(body["goodTilTime"]), "IOC")
            self.assertEqual(payload["t"], 2)
            self.assertEqual(payload["r"], 1)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC)).verify(
                bytes.fromhex(request.headers["X-Signature"]), canonical_json(payload))
            return httpx.Response(200, json={"orderId": "risk-1", "status": "CANCELED",
                "originalSize": "0.001", "remainingSize": "0.0005", "avgFillPrice": "49980"})
        result = self.client(handler).emergency_reduce(MARKET, "sell", "0.001", "risk-exit")
        self.assertEqual(result["filledSize"], "0.0005")
        self.assertEqual(result["status"], "CANCELED")

    def test_reduce_only_min_notional_exemption_does_not_invent_min_size_exemption(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(202, json={"orderId": "risk-1", "status": "ACK"})
        client = self.client(handler)
        # Native API explicitly exempts reduce-only from opening notional, not min size.
        market = {**MARKET, "minOrderNotional": "100"}
        client.emergency_reduce(market, "sell", "0.0001", "risk-exit")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ArcusError) as raised:
            client.emergency_reduce(market, "sell", "0.00001", "risk-dust")
        self.assertEqual(raised.exception.code, "MIN_ORDER_SIZE")
        self.assertEqual(len(calls), 1)

    def test_stop_status_never_invents_cancelled_trigger_or_child_id(self):
        for status, filled, expected in [("TPSL_PLACED", "0", False),
                                         ("TPSL_TRIGGERED", "0", True),
                                         ("TPSL_CANCELED", "0", None),
                                         ("FILLED", "0.1", True)]:
            with self.subTest(status=status):
                client = self.client(lambda request: httpx.Response(200, json={"orderId": "stop-1",
                    "status": status, "filledSize": filled, "updatedAt": 1712341800000000,
                    "parentOrderId": "entry-1"}))
                result = client.stop_status(MARKET, "native-stop", "stop-1")
                self.assertEqual(result["triggered"], expected)
                self.assertIsNone(result["childOrderId"])
                self.assertEqual(result["triggeredAt"], 1712341800000000 if status == "TPSL_TRIGGERED" else None)

    def test_funding_ledger_is_public_microseconds_signed_and_available_when_flat(self):
        funding = {"marketId": 1, "marketDisplayName": "BTC-USD", "size": "0.1",
                   "fundingRate": "0.0001", "payment": "-0.5", "time": 1712343600000000}
        def handler(request):
            self.assertEqual(request.url.path, "/v1/funding")
            self.assertEqual(request.url.params["market"], "1")
            self.assertEqual(request.url.params["from"], "1712341800000000")
            self.assertEqual(request.url.params["to"], "1712345400000000")
            return httpx.Response(200, json={"fundingPayments": [funding], "total": 1})
        result = self.client(handler).funding_payments(MARKET, 1712341800000000, 1712345400000000)
        self.assertEqual(result, [funding])
        self.assertEqual(result[0]["payment"], "-0.5")
        client = self.client(lambda request: httpx.Response(200, json={}))
        with self.assertRaises(ArcusError):
            client.funding_payments(MARKET)
        with self.assertRaises(ArcusError):
            client.funding_payments(MARKET, 1712341800)


if __name__ == "__main__":
    unittest.main()
