from decimal import Decimal
import unittest
import time

import test_grid as fixtures


class SubmissionTests(unittest.TestCase):
    setUp = fixtures.GridTests.setUp
    strategy = fixtures.GridTests.strategy
    set_price = fixtures.GridTests.set_price
    def test_unknown_gateway_response_without_id_freezes_other_entries(self):
        self.exchange.place_order = lambda *args, **kwargs: {'status': 'UNKNOWN'}
        strategy = self.strategy()
        self.set_price('115')
        self.engine.start(strategy, self.account, self.market)
        assert strategy['suspended']
        assert strategy['status'] == 'recovering'
        assert sum(s['entry'] is not None for s in strategy['slots']) == 1

    def test_explicit_zero_fill_rejection_is_terminal_and_not_lost(self):
        self.exchange.place_order = lambda *args, **kwargs: {
            'status': 'REJECTED', 'rejectionReason': 'POST_ONLY_WOULD_CROSS', 'filledSize': None}
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        order = strategy['slots'][0]['entry']
        assert order['status'] == 'REJECTED'
        assert order['filledSize'] == '0'
        assert strategy['inventory'] == '0'
        assert not strategy['suspended']

    def test_rejection_without_fill_evidence_retains_uncertainty(self):
        self.exchange.place_order = lambda *args, **kwargs: {
            'status': 'REJECTED', 'rejectionReason': 'OPEN_INTEREST_CAP_EXCEEDED'}
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        assert strategy['status'] == 'recovering'
        assert strategy['slots'][0]['entry']['uncertain']
        assert strategy['slots'][0]['entry']['status'] == 'UNKNOWN'

    def test_ack_timeout_freezes_and_never_places_a_duplicate(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        order = strategy['slots'][0]['entry']
        order['submittedAt'] -= 31000
        self.exchange.statuses[order['clientId']] = {'status': 'ACK'}
        self.set_price('115')
        self.engine.tick(strategy, self.account, self.market)
        assert strategy['suspended']
        assert len(self.exchange.placed) == 1

    def test_client_id_is_bounded_after_arbitrarily_many_cycles(self):
        strategy = self.strategy()
        slot = strategy['slots'][0]
        slot['cycle'] = 10 ** 50
        assert len(self.engine._new_order(strategy, slot, 'entry', 'BUY', Decimal('1'))['clientId']) <= 36

    def test_manual_orders_freeze_only_panel_entries(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        self.account['snapshot']['orders'] = [{'market': 'TEST-USD', 'clientId': 'manual-order', 'orderId': 'manual-id'}]
        self.engine.tick(strategy, self.account, self.market)
        assert strategy['suspended']
        assert all(client_id != 'manual-order' for client_id, _ in self.exchange.canceled)

    def test_later_account_position_mismatch_freezes_new_entries(self):
        strategy = self.strategy()
        self.engine.start(strategy, self.account, self.market)
        strategy['lastInventoryChangeAt'] = int(time.time() * 1000) - 20000
        self.account['snapshot']['positions'] = [{'market': 'TEST-USD', 'size': '2', 'side': 'LONG'}]
        self.set_price('115')
        self.engine.tick(strategy, self.account, self.market)
        assert strategy['suspended']
        assert len(self.exchange.placed) == 1
