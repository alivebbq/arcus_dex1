"""Quant arithmetic parity and fake-only Arcus trend execution safety."""
import ast
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
import time
import unittest
from unittest.mock import patch

import test_grid as fixtures
from backend.trend import Bar, HOUR_MS, RULES, SuperTrendIndicators, SuperTrendReading, TrendEngine, closed_bars, entry_plan


class TrendExchange(fixtures.Exchange):
    def __init__(self, store, account, clock):
        super().__init__(store)
        self.account_data, self.clock = account, clock
        self.timeline = []
        self.stop_placed = []
        self.emergencies = []
        self.fill_rows = []
        self.funding_rows = []
        self.stop_failure = None
        self.current_margin = {"leverage": 5, "marginMode": "ISOLATED"}

    def margin_config(self, market):
        return deepcopy(self.current_margin)

    def configure_margin(self, market, leverage, marginMode):
        self.current_margin = {"leverage": leverage, "marginMode": marginMode.upper()}
        return {"verified": True, "status": "APPLIED", "current": self.current_margin, "requestId": "config-1"}

    def account_snapshot(self):
        result = deepcopy(self.account_data["snapshot"])
        result.update(connected=True, updatedAt=self.clock())
        return result

    def place_stop_loss(self, market, side, stop_price, limit_price, client_id):
        saved = next(strategy["protection"] for strategy in self.store.strategies.values()
                     if strategy.get("protection") and strategy["protection"]["clientId"] == client_id)
        assert saved["state"] == "pending_submission"
        assert saved["positionTpsl"] and saved["size"] == "0"
        self.timeline.append("protection")
        if self.stop_failure:
            raise self.stop_failure
        order = {"clientId": client_id, "orderId": f"stop-{len(self.stop_placed) + 1}",
                 "price": limit_price, "stopPrice": stop_price, "status": "UNTRIGGERED", "filledSize": "0"}
        self.stop_placed.append(order)
        self.statuses[client_id] = deepcopy(order)
        return {"orderId": order["orderId"], "status": "ACK"}

    def stop_status(self, market, client_id, order_id=None):
        result = self.order_status(market, client_id, order_id)
        result["triggered"] = result["status"] == "TPSL_TRIGGERED"
        return result

    def cancel_order(self, market, client_id, order_id=None):
        self.timeline.append("cancel")
        # Extra records are separate from the grid fixture's normal slots.
        self.canceled.append((client_id, order_id))
        return {"status": "CANCEL_ACKNOWLEDGED"}

    def emergency_reduce(self, market, side, size, client_id):
        saved = next(strategy["emergency"] for strategy in self.store.strategies.values()
                     if strategy.get("emergency") and strategy["emergency"]["clientId"] == client_id)
        assert saved["state"] == "pending_submission" and saved["reduceOnly"]
        order = {"clientId": client_id, "orderId": f"emergency-{len(self.emergencies) + 1}",
                 "side": side, "size": size, "filledSize": "0", "status": "OPEN"}
        self.emergencies.append(order)
        order["referenceMark"] = market["markPrice"]
        self.statuses[client_id] = deepcopy(order)
        return {"orderId": order["orderId"], "status": "ACK"}

    def fills(self, *args, **kwargs):
        return deepcopy(self.fill_rows)

    def funding_payments(self, *args, **kwargs):
        if self.funding_rows is None:
            raise RuntimeError("funding unavailable")
        return deepcopy(self.funding_rows)


class TrendTests(unittest.TestCase):
    def setUp(self):
        fixtures.GridTests.setUp(self)
        self.clock_value = int(time.time() * 1000) // HOUR_MS * HOUR_MS + 20000
        self.clock = lambda: self.clock_value
        self.clock_patch = patch("backend.trend.now_ms", self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        first = self.clock_value // HOUR_MS * HOUR_MS - 200 * HOUR_MS
        candles = [{"time": (first + i * HOUR_MS) // 1000, "open": "100", "high": "101", "low": "99", "close": "100"}
                   for i in range(200)]
        self.market.update(symbol="ETH-USD", marketId=2, initialMarginFraction="0.04",
                           offHoursInitialMarginFraction="0.04", regularTradingHours=None, isOutsideRth=False,
                           makerFee="0", takerFee="0.000225", markPrice="100", bestBid="99", bestAsk="101",
                           stepSize="0.001", minOrderSize="0.001", candles=candles,
                           candlesUpdatedAt=self.clock_value, availableForStrategy="1000")
        self.account["snapshot"].update(updatedAt=self.clock_value, availableBalance="1000")
        self.config = {"accountId": self.account["id"], "market": "ETH-USD", "budget": "1000",
                       "mode": "live", "leverage": 5, "marginMode": "isolated", "name": "trend"}
        self.exchange = TrendExchange(self.store, self.account, self.clock)
        self.engine = TrendEngine(self.store, lambda account: self.exchange)

    def strategy(self, **changes):
        return self.engine.create({**self.config, **changes}, self.market)

    def append_bar(self, close=130, high=135, low=99):
        prior = self.market["candles"][-1]
        stamp = prior["time"] + HOUR_MS // 1000
        self.market["candles"].append({"time": stamp, "open": prior["close"], "high": str(high),
                                        "low": str(low), "close": str(close)})
        self.clock_value = (stamp * 1000 + HOUR_MS) + 20000
        self.market.update(markPrice=str(close), bestBid=str(close - 1), bestAsk=str(close + 1),
                           candlesUpdatedAt=self.clock_value)
        self.account["snapshot"]["updatedAt"] = self.clock_value

    def enter(self, mode="live"):
        strategy = self.strategy(mode=mode)
        self.engine.start(strategy, self.account, self.market)
        self.append_bar()
        self.engine.tick(strategy, self.account, self.market)
        self.assertIsNotNone(strategy["slots"][0]["entry"])
        return strategy

    def fill_entry(self, strategy, size="0.4", terminal="CANCELED"):
        entry = strategy["slots"][0]["entry"]
        self.exchange.statuses[entry["clientId"]].update(status=terminal, filledSize=size, avgFillPrice=entry["price"])
        self.exchange.fill_rows = [{"tradeId": "fill-1", "orderId": entry["orderId"], "size": size,
                                    "price": entry["price"], "fee": "0.1", "closedPnl": "-0.1",
                                    "createdAt": self.clock_value * 1000}]
        self.account["snapshot"]["positions"] = [{"marketId": 2, "size": size, "side": "LONG",
                                                    "markPrice": self.market["markPrice"], "unrealizedPnl": "999"}]

    def test_indicator_exact_parity_with_verified_quant_source(self):
        source = Path(__file__).parent / "fixtures/quant_supertrend_reference.py"
        # Minimal, credential-free extraction of the separately verified source.
        self.assertEqual(hashlib.sha256(source.read_text(encoding="utf-8").encode()).hexdigest().upper(),
                         "6337C35BDD4B592592CD5A1CF5B8AB8C7797D4423D02B6888E4425820847CD89")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        defs = [node for node in tree.body if isinstance(node, ast.ClassDef)
                and node.name in ("SuperTrendIndicators", "SuperTrendReading")]
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        namespace = {"dataclass": dataclass, "isfinite": math.isfinite, "HOUR_MS": HOUR_MS, "Bar": Bar}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *defs], type_ignores=[])), "quant-pure-indicator", "exec"), namespace)
        reference = namespace["SuperTrendIndicators"](multiplier=4.0)
        actual = SuperTrendIndicators()
        rng, previous = random.Random(5285), Decimal("2500")
        for i in range(1000):
            close = previous + Decimal(str(rng.uniform(-40, 40)))
            high = max(previous, close) + Decimal(str(rng.uniform(0, 15)))
            low = min(previous, close) - Decimal(str(rng.uniform(0, 15)))
            bar = Bar(i * HOUR_MS, (i + 1) * HOUR_MS - 1, previous, high, low, close)
            self.assertEqual(asdict(actual.update(bar)), asdict(reference.update(bar)))
            previous = close

    def test_indicator_json_restart_is_identical_and_rejects_gaps_or_unclosed(self):
        engine = SuperTrendIndicators()
        bars = closed_bars(self.market["candles"], self.clock())
        for bar in bars[:100]:
            engine.update(bar)
        restored = SuperTrendIndicators.restore(json.loads(json.dumps(engine.export())))
        for bar in bars[100:]:
            self.assertEqual(engine.update(bar), restored.update(bar))
        with self.assertRaises(ValueError):
            engine.update(bars[-1])
        with self.assertRaises(ValueError):
            engine.update(Bar(bars[-1].open_time_ms + 2 * HOUR_MS, bars[-1].close_time_ms + 2 * HOUR_MS,
                              Decimal(100), Decimal(101), Decimal(99), Decimal(100)))

    def test_source_signal_math_uses_strict_adx_and_real_ohlc_ha_center(self):
        engine = SuperTrendIndicators()
        for bar in closed_bars(self.market["candles"], self.clock()):
            reading = engine.update(bar)
        self.assertIsNone(reading.adx)
        self.append_bar()
        reading = engine.update(closed_bars(self.market["candles"], self.clock())[-1])
        self.assertEqual((reading.trend, reading.flip, reading.entry_signal), (1, 1, 1))
        self.assertEqual(reading.adx, 100)

    def test_planned_risk_collateral_and_offsets_follow_reference_rules(self):
        plan = entry_plan(1, Decimal(130), Decimal(4), Decimal(1000), Decimal(1000), Decimal(150), self.market)
        self.assertEqual(plan["limit"], "129.5")
        self.assertEqual(plan["stop"], "115.5")
        self.assertLessEqual(Decimal(plan["plannedLoss"]), Decimal(30))
        self.assertLessEqual(Decimal(plan["requiredCollateral"]), Decimal(1000))
        short = entry_plan(-1, Decimal(130), Decimal(4), Decimal(1000), Decimal(1000), Decimal(150), self.market)
        self.assertEqual(short["limit"], "130.5")
        self.assertEqual(short["stop"], "144.5")
        with self.assertRaises(ValueError):
            entry_plan(1, Decimal(130), Decimal(4), Decimal("0.1"), Decimal("0.1"), Decimal("0.015"), self.market)

    def test_preview_advisory_allows_small_account_draft_and_structural_errors_reject(self):
        strategy = self.strategy(budget="0.1")
        preview = self.engine.preview(strategy, self.market)
        self.assertEqual(preview["errors"], [])
        self.assertTrue(preview["warnings"])
        self.assertFalse(preview["eligible"])
        for change in ({"budget": "0"}, {"market": "BTC-USD"}, {"leverage": 6}, {"marginMode": "bad"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.strategy(**change)

    def test_bootstrap_historical_signals_and_unfinished_hour_never_enter(self):
        self.append_bar()
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.assertFalse(self.exchange.placed)
        self.assertEqual(strategy["signal"]["entry_signal"], 1)
        last = self.market["candles"][-1]
        self.market["candles"].append({**last, "time": last["time"] + HOUR_MS // 1000})
        self.engine.tick(strategy, self.account, self.market)
        self.assertFalse(self.exchange.placed)

    def test_signal_consumed_before_send_and_restart_never_duplicates_entry(self):
        strategy = self.enter()
        entry = strategy["slots"][0]["entry"]
        self.assertEqual(len(self.exchange.placed), 1)
        saved = self.store.strategies[strategy["id"]]
        self.assertEqual(saved["lastSignalClosedAt"], strategy["trade"]["signalClosedAt"])
        self.assertIn(entry["clientId"], saved["orderRegistry"])
        restarted = deepcopy(saved)
        self.engine.tick(restarted, self.account, self.market)
        self.engine.tick(restarted, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 1)

    def test_native_position_stop_is_persisted_before_partial_entry_cancel(self):
        strategy = self.enter()
        self.fill_entry(strategy, terminal="PARTIALLY_FILLED")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(self.exchange.timeline[:2], ["protection", "cancel"])
        self.assertEqual(len(self.exchange.stop_placed), 1)
        self.assertEqual(strategy["protection"]["size"], "0")
        self.assertEqual(strategy["protection"]["timeInForce"], "GTT")
        self.assertEqual(Decimal(strategy["inventory"]), Decimal("0.4"))

    def test_entry_timeout_cancels_owned_only_and_does_not_retry_old_signal(self):
        strategy = self.enter()
        entry = strategy["slots"][0]["entry"]
        self.clock_value += 60001
        self.engine.tick(strategy, self.account, self.market)
        self.assertIn((entry["clientId"], entry["orderId"]), self.exchange.canceled)
        self.exchange.statuses[entry["clientId"]].update(status="CANCELED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        self.assertIsNone(strategy["trade"])
        self.assertEqual(len(self.exchange.placed), 1)

    def test_pause_retains_native_stop_and_existing_reduce_exit(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        self.engine._exit(strategy, self.market, self.exchange, "opposite_qualified_signal")
        exit_id = strategy["slots"][0]["exit"]["clientId"]
        stop_id = strategy["protection"]["clientId"]
        self.exchange.canceled.clear()
        self.engine.pause(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "paused")
        self.assertFalse(any(identity in (exit_id, stop_id) for identity, _ in self.exchange.canceled))
        self.assertIsNotNone(strategy["protection"])

    def test_stop_timeout_requires_terminal_cancel_then_fresh_exact_remainder(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        protection = strategy["protection"]
        row = self.exchange.statuses[protection["clientId"]]
        row.update(status="TPSL_TRIGGERED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        first_trigger = protection["firstTriggeredAt"]
        self.clock_value += 10001
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(protection["firstTriggeredAt"], first_trigger)
        self.assertFalse(self.exchange.emergencies)
        row.update(status="CANCELED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.emergencies), 1)
        self.assertEqual(self.exchange.emergencies[0]["size"], "0.4")
        self.assertEqual(self.exchange.emergencies[0]["side"], "SELL")

    def test_daily_risk_uses_owned_price_cost_not_exchange_margin_delta(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        # The API margin-delta fixture is +999. Owned price PnL is +0.2.
        self.assertEqual(Decimal(strategy["dayRisk"]["pnl"]), Decimal("0.1"))
        self.assertEqual(strategy["stats"]["fees"], "0.1")
        self.assertEqual(strategy["stats"]["netRealizedPnl"], "-0.1")
        self.exchange.funding_rows = [{"marketId": 2, "time": self.clock_value * 1000, "payment": "-0.3"}]
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["dayRisk"]["pnl"]), Decimal("-0.2"))

    def test_missing_fee_funding_or_fill_coverage_blocks_new_risk_without_guessing_zero(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.exchange.fill_rows = []
        self.engine.tick(strategy, self.account, self.market)
        self.assertFalse(strategy["accounting"]["known"])
        self.assertEqual(strategy["dayRisk"]["remaining"], "0")
        self.fill_entry(strategy)
        self.exchange.funding_rows = None
        self.engine.tick(strategy, self.account, self.market)
        self.assertFalse(strategy["accounting"]["known"])

    def test_daily_halt_survives_resume_without_reset(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.exchange.fill_rows[0]["closedPnl"] = "-200"
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["dayRisk"]["halted"])
        before = deepcopy(strategy["dayRisk"])
        self.engine._read_accounting(strategy, self.exchange, self.market, self.account["snapshot"])
        self.assertTrue(strategy["dayRisk"]["halted"])
        self.assertEqual(strategy["dayRisk"]["baseline"], before["baseline"])

    def test_paper_prices_cross_after_observation_and_no_api_writes(self):
        strategy = self.enter(mode="paper")
        self.market.update(markPrice="129", bestBid="128", bestAsk="130")
        self.engine.tick(strategy, self.account, self.market)
        self.assertGreater(Decimal(strategy["inventory"]), 0)
        self.engine._exit(strategy, self.market, None, "opposite_qualified_signal")
        self.market.update(markPrice="131", bestBid="130", bestAsk="132")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(strategy["inventory"], "0")
        self.assertFalse(self.exchange.placed)
        self.assertFalse(self.exchange.stop_placed)
        self.assertFalse(self.exchange.emergencies)

    def test_failed_native_protection_schedules_safe_reduction_and_never_leaves_ignored_inventory(self):
        class DefiniteStopRejection(Exception):
            uncertain = False
        strategy = self.enter()
        self.fill_entry(strategy)
        self.exchange.stop_failure = DefiniteStopRejection("STOP_TRIGGER_ALREADY_REACHED")
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["suspended"])
        self.assertEqual(strategy["protection"]["status"], "REJECTED")
        self.assertEqual(len(self.exchange.emergencies), 1)
        self.assertEqual(self.exchange.emergencies[0]["size"], "0.4")
        self.assertTrue(strategy["emergency"]["reduceOnly"])
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.emergencies), 1)

    def test_ambiguous_protection_is_never_retried_or_discarded_when_regular_exit_flattens(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.exchange.stop_failure = fixtures.UncertainWrite("stop write timed out")
        self.engine.tick(strategy, self.account, self.market)
        protection_id = strategy["protection"]["clientId"]
        exit_order = strategy["slots"][0]["exit"]
        self.exchange.statuses[exit_order["clientId"]].update(status="FILLED", filledSize="0.4", avgFillPrice=exit_order["price"])
        self.account["snapshot"]["positions"] = []
        self.engine.tick(strategy, self.account, self.market)
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(self.exchange.timeline.count("protection"), 1)
        self.assertIsNotNone(strategy["trade"])
        self.assertEqual(strategy["protection"]["clientId"], protection_id)
        self.assertTrue(any(order["clientId"] == protection_id for order in strategy["orders"]))

    def test_qualified_opposite_signal_exits_without_immediate_reverse(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        self.clock_value += HOUR_MS
        self.market.update(markPrice="140", bestBid="139", bestAsk="141", candlesUpdatedAt=self.clock_value)
        self.account["snapshot"]["updatedAt"] = self.clock_value
        closed_at = self.clock_value // HOUR_MS * HOUR_MS
        reading = SuperTrendReading(closed_at - HOUR_MS, 4.0, 30.0, -1, -1, -1)
        bar = Bar(reading.open_time_ms, closed_at - 1, Decimal(130), Decimal(141), Decimal(129), Decimal(140))
        with patch.object(self.engine, "_update_indicator", return_value=[(reading, bar)]):
            self.engine.tick(strategy, self.account, self.market)
            self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 2)
        self.assertEqual(strategy["direction"], "long")
        self.assertTrue(self.exchange.placed[-1]["reduceOnly"])
        self.assertEqual(strategy["trade"]["exitReason"], "opposite_qualified_signal")

    def test_paused_2r_breakeven_is_server_only_and_initial_exchange_stop_is_retained(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        initial_stop = strategy["protection"]["stopPrice"]
        self.engine.pause(strategy, self.account, self.market)
        self.append_bar(close=180, high=185, low=129)
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "paused")
        self.assertTrue(strategy["trade"]["breakevenActivated"])
        self.assertEqual(strategy["trade"]["breakevenScope"], "server_only_initial_exchange_stop_retained")
        self.assertEqual(strategy["protection"]["stopPrice"], initial_stop)
        self.assertEqual(len(self.exchange.stop_placed), 1)
        self.assertGreater(Decimal(strategy["trade"]["activeStop"]), Decimal(strategy["trade"]["limit"]))

    def test_reservation_uses_initial_margin_not_isolated_cash_pool(self):
        strategy = self.strategy()
        strategy["hasStarted"] = True
        strategy["status"] = "running"
        self.account["snapshot"]["positions"] = [{"marketId": 2, "size": "1", "side": "LONG",
                                                    "markPrice": "100", "marginUsed": "900"}]
        self.assertEqual(self.engine.remaining_collateral(strategy, self.account, self.market), Decimal("980"))

    def test_protective_trigger_tightens_existing_exit_and_later_signal_cannot_loosen_it(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        self.engine._exit(strategy, self.market, self.exchange, "opposite_qualified_signal")
        exit_id = strategy["slots"][0]["exit"]["clientId"]
        self.clock_value += 1000
        self.engine._exit(strategy, self.market, self.exchange, "breakeven_stop", urgent=True)
        self.assertTrue(strategy["trade"]["urgent"])
        deadline = strategy["trade"]["exitRequestedAt"] + RULES["stopTimeoutMs"]
        self.engine._exit(strategy, self.market, self.exchange, "opposite_qualified_signal")
        self.assertTrue(strategy["trade"]["urgent"])
        self.assertEqual(strategy["trade"]["exitRequestedAt"] + RULES["stopTimeoutMs"], deadline)
        self.assertEqual(strategy["slots"][0]["exit"]["clientId"], exit_id)

    def test_confirmed_daily_halt_cancels_protection_then_reduces_fresh_owned_remainder(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.exchange.fill_rows[0]["closedPnl"] = "-200"
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["dayRisk"]["halted"])
        self.assertIsNotNone(strategy["dayRisk"]["haltConfirmedAt"])
        self.assertEqual(strategy["trade"]["exitReason"], "daily_loss_limit")
        protection = strategy["protection"]
        self.assertIn((protection["clientId"], protection["orderId"]), self.exchange.canceled)
        self.assertFalse(self.exchange.emergencies)
        self.assertEqual(len(self.exchange.placed), 1)
        # A cancel ACK is insufficient: the strategy still waits on exact terminal evidence.
        self.engine.tick(strategy, self.account, self.market)
        self.assertFalse(self.exchange.emergencies)
        self.exchange.statuses[protection["clientId"]].update(status="CANCELED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.emergencies), 1)
        self.assertEqual(self.exchange.emergencies[0]["size"], "0.4")
        self.assertEqual(self.exchange.emergencies[0]["side"], "SELL")
        # A later unknown ledger cannot erase a previously confirmed daily halt.
        self.exchange.funding_rows = None
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["dayRisk"]["halted"])
        self.assertEqual(len(self.exchange.emergencies), 1)

    def test_unknown_daily_ledger_blocks_entry_without_forcing_position_flat(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.exchange.funding_rows = None
        self.engine.tick(strategy, self.account, self.market)
        self.assertFalse(strategy["dayRisk"]["known"])
        self.assertFalse(strategy["dayRisk"]["halted"])
        self.assertEqual(strategy["dayRisk"]["remaining"], "0")
        self.assertNotIn("exitReason", strategy["trade"])
        self.assertFalse(self.exchange.emergencies)
        self.assertIsNone(strategy["slots"][0]["exit"])
        self.assertEqual(strategy["protection"]["status"], "UNTRIGGERED")
        self.assertFalse(self.exchange.canceled)

    def test_paper_day_roll_anchors_open_unrealized_once(self):
        strategy = self.enter(mode="paper")
        self.market.update(markPrice="129", bestBid="128", bestAsk="130")
        self.engine.tick(strategy, self.account, self.market)
        size = Decimal(strategy["inventory"])
        self.assertGreater(size, 0)
        self.market.update(markPrice="130", bestBid="129", bestAsk="131")
        # Move to the following Shanghai calendar day while retaining the owned position.
        self.clock_value += 86_400_000
        self.engine._read_accounting(strategy, None, self.market, self.account["snapshot"])
        self.assertEqual(Decimal(strategy["dayRisk"]["pnl"]), Decimal(0))
        anchor = strategy["dayRisk"]["unrealizedAnchor"]
        self.market["markPrice"] = "132"
        self.engine._read_accounting(strategy, None, self.market, self.account["snapshot"])
        self.assertEqual(Decimal(strategy["dayRisk"]["pnl"]), size * Decimal(2))
        self.assertEqual(strategy["dayRisk"]["unrealizedAnchor"], anchor)

    def test_live_risk_exit_uses_fresh_position_mark_when_public_quote_is_unavailable(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        protection = strategy["protection"]
        self.exchange.statuses[protection["clientId"]].update(status="TPSL_TRIGGERED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        self.clock_value += 10001
        self.market.update(markPrice="0", bestBid=None, bestAsk=None)
        self.account["snapshot"]["positions"][0].update(markPrice="127.3")
        self.engine.tick(strategy, self.account, self.market)
        self.assertIn((protection["clientId"], protection["orderId"]), self.exchange.canceled)
        self.assertFalse(self.exchange.emergencies)
        self.exchange.statuses[protection["clientId"]].update(status="CANCELED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.emergencies), 1)
        self.assertEqual(self.exchange.emergencies[0]["size"], "0.4")
        self.assertEqual(self.exchange.emergencies[0]["referenceMark"], "127.3")
        self.assertEqual(strategy["emergency"]["priceReference"], "fresh_owned_position_mark")

    def test_risk_exit_without_any_authoritative_mark_preserves_existing_native_stop(self):
        strategy = self.enter()
        self.fill_entry(strategy)
        self.engine.tick(strategy, self.account, self.market)
        protection = strategy["protection"]
        self.exchange.statuses[protection["clientId"]].update(status="TPSL_TRIGGERED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        self.clock_value += 10001
        self.market.update(markPrice="0", bestBid=None, bestAsk=None)
        self.account["snapshot"]["positions"][0].update(markPrice="0")
        self.exchange.canceled.clear()
        self.engine.tick(strategy, self.account, self.market)
        self.assertFalse(self.exchange.emergencies)
        self.assertFalse(any(client_id == protection["clientId"] for client_id, _ in self.exchange.canceled))
        self.assertIsNone(protection["cancelRequestedAt"])
        self.assertTrue(strategy["suspended"])
        self.assertIn("标记价", strategy["error"])


if __name__ == "__main__":
    unittest.main()
