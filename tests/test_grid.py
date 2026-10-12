"""Execution safety tests use a fake matching engine; no exchange writes."""
from copy import deepcopy
from decimal import Decimal
import time
import unittest

from backend.grid import GridEngine, quantize_price


class MemoryStore:
    def __init__(self):
        self.strategies = {}
        self.events = []

    def save_strategy(self, strategy):
        self.strategies[strategy["id"]] = deepcopy(strategy)

    def event(self, level, message, strategyId=None):
        self.events.append((level, message, strategyId))


class UncertainWrite(Exception):
    uncertain = True


class Exchange:
    def __init__(self, store):
        self.store = store
        self.placed = []
        self.canceled = []
        self.statuses = {}
        self.lookups = []
        self.timeout = False
        self.read_error = False

    def place_order(self, market, side, price, size, client_id, reduce_only=False):
        saved_order = next(order for strategy in self.store.strategies.values()
                           for slot in strategy["slots"]
                           for order in (slot["entry"], slot["exit"])
                           if order and order["clientId"] == client_id)
        assert saved_order["state"] == "pending_submission"
        assert saved_order["status"] == "UNKNOWN"
        assert saved_order["timeInForce"] == "ALO"
        order_id = f"order-{len(self.placed) + 1}"
        order = {"clientId": client_id, "orderId": order_id, "side": side,
                 "price": price, "size": size, "filledSize": "0",
                 "status": "OPEN", "reduceOnly": reduce_only}
        self.placed.append(order)
        self.statuses[client_id] = deepcopy(order)
        if self.timeout:
            raise UncertainWrite("network timed out after send")
        # Even a FILLED acknowledgement must not drive inventory accounting.
        return {"orderId": order_id, "clientId": client_id, "status": "FILLED", "filledSize": size}

    def cancel_order(self, market, client_id, order_id=None):
        saved = next(order for strategy in self.store.strategies.values()
                     for slot in strategy["slots"] for order in (slot["entry"], slot["exit"])
                     if order and order["clientId"] == client_id)
        assert saved["state"] == "cancel_pending"
        self.canceled.append((client_id, order_id))
        return {"status": "CANCELED"}  # Ack is not terminal confirmation.

    def order_status(self, market, client_id, order_id=None):
        self.lookups.append((client_id, order_id))
        if self.read_error:
            raise RuntimeError("read offline")
        return deepcopy(self.statuses.get(client_id, {"status": "UNKNOWN"}))


class GridTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.exchange = Exchange(self.store)
        self.engine = GridEngine(self.store, lambda account: self.exchange)
        self.market = {"marketId": 1, "symbol": "TEST-USD", "status": "ONLINE",
                       "tickSize": "0.1", "stepSize": "0.1", "minOrderNotional": "5",
                       "minOrderSize": "0.1", "maxOrderSize": "1000", "markPrice": "105",
                       "bestBid": {"price": "104", "size": "10"},
                       "bestAsk": {"price": "106", "size": "10"}}
        self.config = {"accountId": "account-1", "market": "TEST-USD", "name": "test grid",
                       'budgetType': 'notional',
                       "lower": "100", "upper": "120", "gridCount": 2, "budget": "210",
                       "direction": "long", "mode": "live"}
        self.account = {"id": "account-1", "snapshot": {"connected": True,
                        "updatedAt": int(time.time() * 1000), "availableBalance": "1000",
                        "positions": [], "orders": []}}

    def strategy(self, **changes):
        return self.engine.create({**self.config, **changes}, self.market)

    def set_price(self, price):
        self.market.update(markPrice=str(price), bestBid=str(Decimal(str(price)) - 1),
                           bestAsk=str(Decimal(str(price)) + 1))

    def test_quantity_reserves_all_entries_and_uses_decimal_step(self):
        result = self.engine.preview({**self.config, "budget": "209.99"}, self.market)
        self.assertEqual(result["errors"], [])
        self.assertEqual(Decimal(result["quantity"]), Decimal("0.9"))
        self.assertEqual(Decimal(result["totalNotional"]), Decimal("189"))
        short = self.engine.preview({**self.config, "direction": "short", "budget": "230"}, self.market)
        self.assertEqual(Decimal(short["quantity"]), Decimal("1"))
        self.assertEqual(short["levels"], ["100", "110", "120"])

    def test_invalid_interval_finite_number_and_order_minimum_are_rejected(self):
        for change in ({"lower": "0"}, {"upper": "100"}, {"gridCount": 2.1},
                       {"gridCount": 51}, {"budget": "NaN"}, {"budget": "Infinity"},
                       {"budget": "1000001"}, {"budget": "0.01"}, {"lower": "100.01", "upper": "100.09"}):
            with self.subTest(change=change):
                self.assertTrue(self.engine.preview({**self.config, **change}, self.market)["errors"])
        market = {**self.market, "minOrderNotional": "101"}
        self.assertTrue(self.engine.preview(self.config, market)["errors"])

    def test_tick_tier_bound_is_exclusive_and_rounding_rechecks_band(self):
        tiers = {**self.market, "tickTiers": [{"upToPrice": "100", "tick": "0.1"}, {"tick": "1"}]}
        self.assertEqual(quantize_price(Decimal("99.99"), tiers), Decimal("99.9"))
        self.assertEqual(quantize_price(Decimal("100.99"), tiers), Decimal("100"))
        self.assertEqual(quantize_price(Decimal("100"), tiers), Decimal("100"))
        # A ceil crossing into a band with a different grid must remain valid.
        from decimal import ROUND_CEILING
        self.assertEqual(quantize_price(Decimal("99.99"), tiers, ROUND_CEILING), Decimal("100"))

    def test_bbo_gates_maker_entry_and_ack_is_never_a_fill(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 1)
        self.assertEqual(self.exchange.placed[0]["price"], "100")
        self.assertFalse(self.exchange.placed[0]["reduceOnly"])
        self.assertEqual(strategy["inventory"], "0")
        self.assertEqual(strategy["slots"][0]["state"], "pending_submission")
        self.assertEqual(strategy["slots"][1]["state"], "idle")

    def test_no_bbo_no_placement_and_mark_zero_is_not_replaced_with_oracle(self):
        strategy = self.strategy()
        market = {**self.market, "bestBid": None, "bestAsk": None}
        self.engine.start(strategy, self.account, market)
        self.assertFalse(self.exchange.placed)
        market.update(markPrice="0", oraclePrice="105")
        self.engine.tick(strategy, self.account, market)
        self.assertTrue(strategy["suspended"])
        self.assertEqual(strategy["status"], "error")

    def test_start_requires_fresh_funded_flat_account_without_foreign_orders(self):
        for change in ({"connected": False}, {"updatedAt": 0}, {"availableBalance": "209"},
                       {"positions": [{"marketId": 1, "size": "0.1"}]},
                       {"orders": [{"marketId": 1, "clientId": "external"}]}):
            with self.subTest(change=change):
                strategy = self.strategy()
                account = {**self.account, "snapshot": {**self.account["snapshot"], **change}}
                with self.assertRaises(ValueError):
                    self.engine.start(strategy, account, self.market)
                self.assertEqual(strategy["status"], "draft")
        self.assertFalse(self.exchange.placed)

    def test_persistence_before_send_and_restart_does_not_duplicate(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        original_id = strategy["slots"][0]["entry"]["clientId"]
        restart = deepcopy(self.store.strategies[strategy["id"]])
        engine = GridEngine(self.store, lambda account: self.exchange)
        engine.tick(restart, self.account, self.market)
        engine.tick(restart, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 1)
        self.assertEqual(restart["slots"][0]["entry"]["clientId"], original_id)
        self.assertIn((original_id, "order-1"), self.exchange.lookups)

    def test_ambiguous_write_never_resubmits_even_when_order_unknown(self):
        self.exchange.timeout = True
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "recovering")
        client_id = self.exchange.placed[0]["clientId"]
        self.exchange.statuses.clear()
        for _ in range(4):
            self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 1)
        self.assertEqual(strategy["slots"][0]["entry"]["clientId"], client_id)
        self.assertEqual(strategy["inventory"], "0")
        self.assertEqual(strategy["status"], "recovering")

    def test_partial_entry_waits_for_cancel_terminal_then_exits_exact_final_fill(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        entry = strategy["slots"][0]["entry"]
        source = self.exchange.statuses[entry["clientId"]]
        source.update(status="PARTIALLY_FILLED", filledSize="0.4", avgFillPrice="100.2")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["inventory"]), Decimal("0.4"))
        self.assertEqual(len(self.exchange.placed), 1)
        self.assertEqual(len(self.exchange.canceled), 1)
        self.assertEqual(strategy["slots"][0]["state"], "cancel_pending")
        # The cancel ACK has not proved the residual entry was removed.
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 1)
        source.update(status="CANCELED", filledSize="0.5")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 2)
        self.assertTrue(self.exchange.placed[1]["reduceOnly"])
        self.assertEqual(self.exchange.placed[1]["side"], "SELL")
        self.assertEqual(self.exchange.placed[1]["size"], "0.5")
        self.assertEqual(Decimal(strategy["inventory"]), Decimal("0.5"))

    def test_partial_exit_profit_uses_cumulative_average_without_double_count(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        source = self.exchange.statuses[strategy["slots"][0]["entry"]["clientId"]]
        source.update(status="CANCELED", filledSize="0.5", avgFillPrice="100.2")
        self.engine.tick(strategy, self.account, self.market)
        exit_order = strategy["slots"][0]["exit"]
        exit_source = self.exchange.statuses[exit_order["clientId"]]
        exit_source.update(status="PARTIALLY_FILLED", filledSize="0.2", avgFillPrice="110.4")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["stats"]["grossProfit"]), Decimal("2.04"))
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["stats"]["grossProfit"]), Decimal("2.04"))
        exit_source.update(status="FILLED", filledSize="0.5", avgFillPrice="110.6")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["stats"]["grossProfit"]), Decimal("5.2"))
        self.assertEqual(strategy["stats"]["cycles"], 1)
        self.assertEqual(strategy["inventory"], "0.0")
        self.assertIsNone(strategy["stats"]["fees"])

    def test_pause_only_cancels_owned_orders_and_keeps_inventory(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        entry = strategy["slots"][0]["entry"]
        self.exchange.statuses[entry["clientId"]].update(status="PARTIALLY_FILLED", filledSize="0.4", avgFillPrice="100")
        self.engine.pause(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "pausing")
        self.assertTrue(all(client.startswith(f"ag-{strategy['id'][:12]}-") for client, _ in self.exchange.canceled))
        self.exchange.statuses[entry["clientId"]].update(status="CANCELED")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "paused")
        self.assertEqual(Decimal(strategy["residualQuantity"]), Decimal("0.4"))
        self.assertEqual(len(self.exchange.placed), 1)
        self.account["snapshot"]["positions"] = [{"marketId": 1, "size": "0.4"}]
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "running")
        self.assertTrue(self.exchange.placed[-1]["reduceOnly"])

    def test_outside_range_does_not_open_but_reduce_only_exit_still_runs(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        source = self.exchange.statuses[strategy["slots"][0]["entry"]["clientId"]]
        source.update(status="FILLED", filledSize="1", avgFillPrice="100")
        self.set_price(90)
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["outOfRange"])
        self.assertEqual(len(self.exchange.placed), 2)
        self.assertTrue(self.exchange.placed[-1]["reduceOnly"])
        self.assertIsNone(strategy["slots"][1]["entry"])

    def test_short_grid_buys_reduce_only_after_sell_is_confirmed_filled(self):
        self.set_price(115)
        strategy = self.strategy(direction="short", budget="230")
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(self.exchange.placed[0]["side"], "SELL")
        self.assertEqual(self.exchange.placed[0]["price"], "120")
        source = self.exchange.statuses[self.exchange.placed[0]["clientId"]]
        source.update(status="FILLED", filledSize="1", avgFillPrice="120")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(self.exchange.placed[1]["side"], "BUY")
        self.assertEqual(self.exchange.placed[1]["price"], "110")
        self.assertTrue(self.exchange.placed[1]["reduceOnly"])

    def test_paper_uses_later_live_crossings_and_never_calls_exchange(self):
        strategy = self.strategy(mode="paper")
        self.account["snapshot"].update(availableBalance="0", connected=False)
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(strategy["inventory"], "0")
        self.set_price(99)
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["inventory"]), Decimal("1"))
        self.assertEqual(strategy["stats"]["cycles"], 0)
        self.set_price(111)
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(strategy["stats"]["cycles"], 1)
        self.assertEqual(Decimal(strategy["stats"]["grossProfit"]), Decimal("10"))
        self.assertEqual(strategy["inventory"], "0")
        self.assertFalse(self.exchange.placed)
        self.assertFalse(self.exchange.canceled)
        self.assertFalse(self.exchange.lookups)

    def test_missing_filled_size_or_read_failure_freezes_entries(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        source = self.exchange.statuses[self.exchange.placed[0]["clientId"]]
        source.pop("filledSize")
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["suspended"])
        self.assertEqual(strategy["status"], "recovering")
        self.assertEqual(len(self.exchange.placed), 1)
        self.exchange.read_error = True
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.placed), 1)

    def test_missing_average_preserves_observed_inventory_without_guessing_cost(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        source = self.exchange.statuses[self.exchange.placed[0]["clientId"]]
        source.update(status="PARTIALLY_FILLED", filledSize="0.4")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["residualQuantity"]), Decimal("0.4"))
        self.assertEqual(strategy["inventory"], "0")
        self.assertEqual(strategy["stats"]["grossProfit"], "0")
        self.assertTrue(strategy["suspended"])
        self.assertEqual(len(self.exchange.placed), 1)
        source.update(status="MARGIN_CANCELED", avgFillPrice="100.1")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["inventory"]), Decimal("0.4"))
        self.assertTrue(self.exchange.placed[-1]["reduceOnly"])

    def test_forced_closure_is_not_invented_as_normal_fill_or_flat_position(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.exchange.statuses[self.exchange.placed[0]["clientId"]].update(status="LIQUIDATED", filledSize="1")
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["suspended"])
        self.assertEqual(strategy["status"], "recovering")
        self.assertEqual(strategy["inventory"], "0")
        self.assertEqual(len(self.exchange.placed), 1)


if __name__ == "__main__":
    unittest.main()
