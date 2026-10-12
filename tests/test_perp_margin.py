"""Perpetual margin sizing and configuration gates; all exchange calls are fake."""
from copy import deepcopy
from decimal import Decimal
import unittest

import test_grid as fixtures
from backend.grid import GridEngine


class MarginExchange(fixtures.Exchange):
    def __init__(self, store, account):
        super().__init__(store)
        self.account_data = account
        self.configured = []
        self.current_margin = {"leverage": 1, "marginMode": "CROSS", "verified": True}
        self.setup_pending = False
        self.setup_error = None
        self.false_confirmation = False

    def configure_margin(self, market, leverage, marginMode):
        saved = next(strategy for strategy in self.store.strategies.values()
                     if strategy.get("budgetType") == "margin")
        assert saved["marginSetup"]["state"] == "pending"
        assert saved["marginSetup"].get("attemptedAt")
        self.configured.append((market["marketId"], leverage, str(marginMode).upper()))
        if self.setup_error:
            raise self.setup_error
        if not self.setup_pending and not self.false_confirmation:
            self.current_margin = {"leverage": leverage, "marginMode": str(marginMode).upper(), "verified": True}
        return {"status": "PENDING" if self.setup_pending else "APPLIED",
                "verified": not self.setup_pending, "pending": self.setup_pending,
                "current": deepcopy(self.current_margin), "requestId": "margin-request-1"}

    def margin_config(self, market):
        return deepcopy(self.current_margin)

    def account(self):
        return deepcopy(self.account_data["snapshot"])

    def account_snapshot(self):
        return self.account()


class PerpetualMarginTests(unittest.TestCase):
    strategy = fixtures.GridTests.strategy
    set_price = fixtures.GridTests.set_price

    def setUp(self):
        fixtures.GridTests.setUp(self)
        self.market.update(initialMarginFraction="0.05", offHoursInitialMarginFraction="0.05",
                           regularTradingHours=None, isOutsideRth=False)
        self.config.update(budgetType="margin", budget="42", leverage=5, marginMode="cross")
        self.exchange = MarginExchange(self.store, self.account)
        self.engine = GridEngine(self.store, lambda account: self.exchange)

    def test_leverage_notional_cap_and_worst_mark_initial_margin(self):
        result = self.engine.preview(self.config, self.market)
        self.assertEqual(result["errors"], [])
        self.assertEqual(Decimal(result["notionalBudget"]), Decimal("210"))
        self.assertEqual(Decimal(result["marginReserve"]), Decimal("2.10"))
        self.assertEqual(Decimal(result["quantity"]), Decimal("0.8"))
        self.assertEqual(Decimal(result["totalNotional"]), Decimal("168"))
        # The exchange uses position size times mark, not sum of limit notionals.
        required_im = Decimal(result["quantity"]) * 2 * Decimal("120") / 5
        self.assertEqual(Decimal(result["requiredCollateral"]), required_im + Decimal("2.1"))
        self.assertLessEqual(Decimal(result["requiredCollateral"]), Decimal(self.config["budget"]))

    def test_short_uses_same_upper_mark_reserve_and_higher_entry_notional(self):
        result = self.engine.preview({**self.config, "direction": "short"}, self.market)
        self.assertEqual(result["errors"], [])
        self.assertEqual(Decimal(result["quantity"]), Decimal("0.8"))
        self.assertEqual(Decimal(result["totalNotional"]), Decimal("184"))
        self.assertEqual(Decimal(result["requiredCollateral"]), Decimal("40.5"))

    def test_higher_current_mark_reduces_quantity_to_fit_collateral(self):
        result = self.engine.preview(self.config, {**self.market, "markPrice": "200"})
        self.assertEqual(result["errors"], [])
        self.assertEqual(Decimal(result["quantity"]), Decimal("0.4"))
        self.assertEqual(Decimal(result["requiredCollateral"]), Decimal("34.1"))

    def test_off_hours_uplift_and_unknown_session_state(self):
        equity_market = {**self.market, "regularTradingHours": {"timezone": "America/New_York"},
                         "offHoursInitialMarginFraction": "0.3", "isOutsideRth": True}
        result = self.engine.preview(self.config, equity_market)
        self.assertEqual(result["errors"], [])
        self.assertEqual(Decimal(result["quantity"]), Decimal("0.5"))
        self.assertEqual(Decimal(result["requiredCollateral"]), Decimal("38.1"))
        unknown = self.engine.preview(self.config, {**equity_market, "isOutsideRth": None})
        self.assertTrue(unknown["errors"])

    def test_max_leverage_integer_mode_and_missing_metadata_validation(self):
        for change in ({"leverage": 21}, {"leverage": "5.1"}, {"leverage": 0},
                       {"leverage": True}, {"leverage": "NaN"}, {"marginMode": "invalid"}):
            with self.subTest(change=change):
                self.assertTrue(self.engine.preview({**self.config, **change}, self.market)["errors"])
        missing = {**self.market}
        missing.pop("initialMarginFraction")
        self.assertTrue(self.engine.preview(self.config, missing)["errors"])
        fractional_cap = {**self.market, "initialMarginFraction": "0.03"}
        self.assertTrue(self.engine.preview({**self.config, "leverage": 34}, fractional_cap)["errors"])
        self.assertEqual(self.engine.preview({**self.config, "leverage": 33}, fractional_cap)["errors"], [])

    def test_small_margin_budget_cannot_bypass_minimum_order_notional(self):
        fine_size = {**self.market, "stepSize": "0.001", "minOrderSize": "0.001"}
        self.assertTrue(self.engine.preview({**self.config, "budget": "0.5"}, fine_size)["errors"])
        fractional = self.engine.preview({**self.config, "budget": "4.04"}, fine_size)
        self.assertEqual(fractional["errors"], [])
        self.assertEqual(Decimal(fractional["quantity"]), Decimal("0.079"))
        self.assertEqual(Decimal(fractional["requiredCollateral"]), Decimal("3.994"))
        self.assertGreaterEqual(Decimal(fractional["orderNotional"]), Decimal("5"))

    def test_configuration_is_durable_before_write_and_exact_before_order(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(self.exchange.configured, [(1, 5, "CROSS")])
        self.assertEqual(strategy["marginSetup"]["state"], "verified")
        self.assertEqual(strategy["status"], "running")
        self.assertTrue(self.exchange.placed)

    def test_ack_is_pending_even_after_restart_and_cannot_place_any_orders(self):
        self.exchange.setup_pending = True
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "configuring")
        self.assertEqual(strategy["marginSetup"]["state"], "pending")
        self.assertFalse(self.exchange.placed)
        restarted = deepcopy(self.store.strategies[strategy["id"]])
        engine = GridEngine(self.store, lambda account: self.exchange)
        for _ in range(3):
            engine.tick(restarted, self.account, self.market)
        self.assertEqual(len(self.exchange.configured), 1)
        self.assertFalse(self.exchange.placed)
        self.exchange.current_margin = {"leverage": 5, "marginMode": "CROSS", "verified": True}
        engine.tick(restarted, self.account, self.market)
        self.assertEqual(restarted["marginSetup"]["state"], "verified")
        self.assertTrue(self.exchange.placed)

    def test_verified_flag_cannot_override_mismatched_leverage_or_mode(self):
        self.exchange.false_confirmation = True
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.assertFalse(self.exchange.placed)
        self.assertNotEqual(strategy["marginSetup"]["state"], "verified")
        self.exchange.current_margin = {"leverage": 5, "marginMode": "ISOLATED", "verified": True}
        self.engine.tick(strategy, self.account, self.market)
        self.assertFalse(self.exchange.placed)

    def test_uncertain_config_write_never_repeats_or_places_orders(self):
        self.exchange.setup_error = fixtures.UncertainWrite("setting write timed out")
        strategy = self.strategy()
        try:
            self.engine.start(strategy, self.account, self.market)
        except fixtures.UncertainWrite:
            pass
        for _ in range(3):
            self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(len(self.exchange.configured), 1)
        self.assertFalse(self.exchange.placed)
        self.assertNotEqual(strategy["marginSetup"]["state"], "verified")

    def test_rejected_configuration_cannot_fall_through_to_live_orders(self):
        class Rejected(Exception):
            uncertain = False
            status_code = 422
            code = "HAS_OPEN_POSITION"
        self.exchange.setup_error = Rejected("HAS_OPEN_POSITION")
        strategy = self.strategy(marginMode="isolated")
        try:
            self.engine.start(strategy, self.account, self.market)
        except Rejected:
            pass
        self.assertFalse(self.exchange.placed)
        self.assertNotEqual(strategy["marginSetup"]["state"], "verified")
        self.assertEqual(strategy["marginSetup"]["state"], "error")

    def test_collateral_is_rechecked_after_delayed_configuration_confirmation(self):
        self.exchange.setup_pending = True
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.account["snapshot"]["availableBalance"] = "20"
        self.exchange.current_margin = {"leverage": 5, "marginMode": "CROSS", "verified": True}
        try:
            self.engine.tick(strategy, self.account, self.market)
        except ValueError:
            pass
        self.assertFalse(self.exchange.placed)
        self.assertNotEqual(strategy["status"], "running")

    def test_resuming_with_external_margin_change_cannot_silently_reconfigure(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        placed_before = len(self.exchange.placed)
        entry = strategy["slots"][0]["entry"]
        self.engine.pause(strategy, self.account, self.market)
        self.exchange.statuses[entry["clientId"]].update(status="CANCELED", filledSize="0")
        self.engine.tick(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "paused")
        self.exchange.current_margin = {"leverage": 1, "marginMode": "ISOLATED", "verified": True}
        try:
            self.engine.start(strategy, self.account, self.market)
        except ValueError:
            pass
        self.assertEqual(len(self.exchange.configured), 1)
        self.assertEqual(len(self.exchange.placed), placed_before)
        self.assertNotEqual(strategy["status"], "running")

    def test_existing_quantity_cannot_bypass_changed_initial_margin_requirement(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(Decimal(strategy["quantity"]), Decimal("0.8"))
        # Price is still inside the band and a second entry could otherwise rest.
        self.set_price(115)
        self.market.update(regularTradingHours={"timezone": "America/New_York"},
                           isOutsideRth=True, offHoursInitialMarginFraction="0.3")
        self.engine.tick(strategy, self.account, self.market)
        self.assertTrue(strategy["suspended"])
        self.assertEqual(len(self.exchange.placed), 1)
        self.assertTrue(self.exchange.canceled)

    def test_draft_margin_quantity_is_revalidated_before_any_live_order(self):
        strategy = self.strategy()
        self.market.update(regularTradingHours={"timezone": "America/New_York"},
                           isOutsideRth=True, offHoursInitialMarginFraction="0.3")
        try:
            self.engine.start(strategy, self.account, self.market)
        except ValueError:
            pass
        if self.exchange.placed:
            quantity = Decimal(self.exchange.placed[0]["size"])
            imr_plus_buffer = quantity * 2 * Decimal("120") * Decimal("0.3") + Decimal("2.1")
            self.assertLessEqual(imr_plus_buffer, Decimal("42"))
        else:
            self.assertNotEqual(strategy["status"], "running")

    def test_paper_leverage_never_writes_account_configuration(self):
        strategy = self.strategy(mode="paper", marginMode="isolated")
        self.account["snapshot"].update(availableBalance="0", connected=False)
        self.engine.start(strategy, self.account, self.market)
        self.assertEqual(strategy["status"], "running")
        self.assertEqual(Decimal(strategy["quantity"]), Decimal("0.8"))
        self.assertFalse(self.exchange.configured)
        self.assertFalse(self.exchange.placed)

    def test_existing_legacy_strategy_without_budget_type_keeps_exact_nominal_quantity(self):
        config = {**self.config, "budgetType": "notional", "budget": "210", "leverage": 1}
        strategy = self.engine.create(config, self.market)
        strategy.pop("budgetType", None)
        strategy.pop("leverage", None)
        strategy.pop("marginMode", None)
        old_quantity = strategy["quantity"]
        # Simulate a pre-migration record that already has its durable id/slots.
        legacy_preview = self.engine.preview(strategy, self.market)
        self.assertEqual(legacy_preview["errors"], [])
        self.assertEqual(Decimal(legacy_preview["quantity"]), Decimal("1"))
        self.assertEqual(strategy["quantity"], old_quantity)


if __name__ == "__main__":
    unittest.main()
