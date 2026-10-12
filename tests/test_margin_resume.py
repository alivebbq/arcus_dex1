import unittest
from decimal import Decimal
import test_perp_margin as fixtures


class MarginResumeTests(unittest.TestCase):
    setUp = fixtures.PerpetualMarginTests.setUp
    strategy = fixtures.PerpetualMarginTests.strategy
    set_price = fixtures.PerpetualMarginTests.set_price

    def test_funded_position_is_not_reserved_twice_on_resume(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        strategy['hasStarted'] = True
        self.account['snapshot']['positions'] = [{'market': 'TEST-USD', 'size': '0.8', 'side': 'LONG', 'markPrice': '105'}]
        # Budget 42, funded IMR16.8; the remaining allocation is25.2, not42.
        self.account['snapshot']['availableBalance'] = '26'
        assert self.engine.remaining_collateral(strategy, self.account, self.market) == Decimal('25.2')
        assert self.engine._refresh_margin_account(strategy, self.account, self.market)

    def test_drop_in_free_collateral_freezes_openings_but_keeps_reduce_only(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        order = strategy['slots'][0]['entry']
        self.exchange.statuses[order['clientId']].update(status='FILLED', filledSize='0.8', avgFillPrice='100')
        self.account['snapshot']['availableBalance'] = '1'
        self.engine.tick(strategy, self.account, self.market)
        assert strategy['suspended']
        assert any(o['reduceOnly'] for o in self.exchange.placed)
