"""Account statistics use fake read-only ledgers; no exchange writes or secrets."""
from copy import deepcopy
from decimal import Decimal
import json

from backend.account_statistics import AccountStatisticsService, aggregate_fills


NOW = 1_900_000_000
BASE_US = NOW * 1_000_000


def fill(identity, stamp, *, size='1', price='100', fee='0.1', pnl='1',
         order=None, market=1, **extra):
    return {'tradeId': str(identity), 'orderId': order or f'order-{identity}',
            'createdAt': stamp, 'marketId': market, 'size': size, 'price': price,
            'fee': fee, 'closedPnl': pnl, **extra}


class Clock:
    def __init__(self):
        self.value = float(NOW)

    def __call__(self):
        return self.value

    def advance(self, seconds=5):
        self.value += seconds


class Ledger:
    def __init__(self, records, index=0, address='test-wallet'):
        self.records = records
        self.account_index = index
        self.address = address
        self.environment = 'mainnet'
        self.api_key = 'private-fixture-do-not-persist'
        self.calls = []
        self.failure = None

    def fills(self, market=None, *, limit=100, from_us=None, to_us=None):
        self.calls.append({'market': market, 'limit': limit, 'from': from_us, 'to': to_us})
        if self.failure:
            raise self.failure
        rows = [row for row in self.records if from_us <= row['createdAt'] <= to_us]
        return deepcopy(sorted(rows, key=lambda row: row['createdAt'], reverse=True)[:limit])


def service(tmp_path, ledger, clock, **kwargs):
    return AccountStatisticsService(tmp_path, lambda account: ledger, clock=clock,
                                    request_interval=0, page_size=3, **kwargs)


def finish(stats, account, clock, maximum=20):
    for _ in range(maximum):
        state = stats.sync_step(account, max_pages=1)
        if state['status'] in ('ready', 'partial', 'error'):
            return state
        clock.advance()
    raise AssertionError('Fixture backfill did not finish')


def test_decimal_all_market_turnover_and_net_fee_pnl_are_separate():
    opening = fill('1', BASE_US - 2, size='2', price='100', fee='1', pnl='-1', market=1)
    closing = fill('2', BASE_US - 1, size='-2', price='110', fee='2', pnl='18', market=2)
    totals = aggregate_fills([opening, closing, deepcopy(opening)])
    assert totals['turnover'] == '420'
    assert totals['fees'] == '3'
    assert totals['realizedPnl'] == '17'
    assert totals['netRealizedPnl'] == '17'
    assert totals['grossRealizedPnl'] == '20'
    assert totals['fillCount'] == 2


def test_trade_id_and_order_id_form_composite_identity_and_rebates_remain_signed():
    first = fill('shared', BASE_US - 1, fee='-0.1', pnl='0.1', order='maker')
    second = fill('shared', BASE_US - 1, fee='0.2', pnl='-0.2', order='taker')
    totals = aggregate_fills([first, second])
    assert totals['turnover'] == '200'
    assert totals['fees'] == '0.1'
    assert totals['netRealizedPnl'] == '-0.1'
    assert totals['grossRealizedPnl'] == '0'
    assert totals['fillCount'] == 2


def test_exact_decimal_small_sizes_do_not_use_float_math():
    totals = aggregate_fills([fill('1', BASE_US - 1, size='0.00000003', price='50000.1',
                                   fee='0.0000001', pnl='0.0000002')])
    assert Decimal(totals['turnover']) == Decimal('0.001500003')
    assert totals['grossRealizedPnl'] == '0.0000003'


def test_snapshot_is_fast_and_never_guesses_zero_before_first_read(tmp_path):
    def forbidden(account):
        raise AssertionError('snapshot may not create a client or request HTTP')
    stats = AccountStatisticsService(tmp_path, forbidden)
    snapshot = stats.snapshot('account-a')
    assert snapshot['status'] == 'syncing'
    assert snapshot['turnover'] is None
    assert snapshot['realizedPnl'] is None
    assert snapshot['fees'] is None
    assert snapshot['complete'] is False
    assert snapshot['stale'] is False  # Unknown statistics have not expired.


def test_unpublished_new_account_is_syncing_without_expiry_warning(tmp_path):
    clock = Clock()
    stats = service(tmp_path, Ledger([]), clock)
    pending = stats.snapshot('brand-new-account')
    assert pending['status'] == 'syncing'
    assert pending['syncing'] is True
    assert pending['stale'] is False
    assert pending['updatedAt'] is None
    assert pending['turnover'] is None


def test_long_backfill_coverage_is_stale_and_schedules_immediate_catch_up(tmp_path):
    clock = Clock()
    ledger = Ledger([fill(identity, BASE_US - identity * 1_000_000) for identity in range(1, 4)])
    stats = service(tmp_path, ledger, clock)
    stats.sync_step({'id': 'account-a'})  # Full page needs its inclusive boundary page.
    clock.advance(1000)
    completed = stats.sync_step({'id': 'account-a'})
    assert completed['complete'] is True
    assert completed['updatedAt'] == int(clock() * 1000)
    assert completed['toTime'] == BASE_US
    assert completed['stale'] is True  # Fresh local completion cannot refresh an old cutoff.
    # No additional refresh_interval delay after finishing old history.
    catch_up = finish(stats, {'id': 'account-a'}, clock)
    assert catch_up['toTime'] > BASE_US
    assert catch_up['stale'] is False


def test_verified_empty_available_history_is_ready_zero(tmp_path):
    clock = Clock()
    ledger = Ledger([])
    stats = service(tmp_path, ledger, clock)
    snapshot = stats.sync_step({'id': 'account-a'})
    assert snapshot['status'] == 'ready'
    assert snapshot['complete'] is True
    assert snapshot['turnover'] == snapshot['fees'] == snapshot['realizedPnl'] == '0'
    assert snapshot['fillCount'] == 0
    assert ledger.calls[0]['market'] is None
    assert ledger.calls[0]['from'] == 100_000_000_000_000


def test_inclusive_backward_pages_deduplicate_boundaries_without_skipping(tmp_path):
    clock = Clock()
    ledger = Ledger([fill(identity, BASE_US - (8 - identity) * 1_000_000) for identity in range(1, 8)])
    stats = service(tmp_path, ledger, clock)
    initial = stats.sync_step({'id': 'account-a'}, max_pages=1)
    assert len(ledger.calls) == 1
    assert initial['complete'] is False
    assert initial['syncedFillCount'] == 3
    assert initial['turnover'] is None
    clock.advance()
    result = finish(stats, {'id': 'account-a'}, clock)
    assert result['fillCount'] == 7
    assert result['turnover'] == '700'
    assert result['fees'] == '0.7'
    assert result['realizedPnl'] == '7'
    assert [row['to'] for row in ledger.calls] == [BASE_US, BASE_US - 3_000_000,
                                                 BASE_US - 5_000_000, BASE_US - 7_000_000]


def test_timestamp_ties_are_paged_inclusively_and_saturation_is_partial(tmp_path):
    clock = Clock()
    stamp = BASE_US - 1_000_000
    ledger = Ledger([fill(index, stamp) for index in range(4)])
    stats = service(tmp_path, ledger, clock)
    stats.sync_step({'id': 'account-a'})
    clock.advance()
    result = stats.sync_step({'id': 'account-a'})
    assert result['status'] == 'partial'
    assert result['complete'] is False
    assert result['fillCount'] is None
    assert result['syncedFillCount'] == 3
    assert result['turnover'] is None
    assert '分页上限' in result['error']
    assert ledger.calls[-1]['to'] == stamp


def test_cache_survives_restart_and_incremental_overlap_does_not_double_count(tmp_path):
    clock = Clock()
    ledger = Ledger([fill(1, BASE_US - 1_000_000)])
    stats = service(tmp_path, ledger, clock)
    assert stats.sync_step({'id': 'account-a'})['fillCount'] == 1
    restored = service(tmp_path, ledger, clock)
    assert restored.snapshot('account-a')['turnover'] == '100'
    assert len(ledger.calls) == 1
    clock.advance(61)
    ledger.records.append(fill(2, int(clock() * 1_000_000) - 1_000_000, size='2'))
    result = restored.sync_step({'id': 'account-a'})
    assert result['status'] == 'ready'
    assert result['fillCount'] == 2
    assert result['turnover'] == '300'
    assert result['fees'] == '0.2'
    assert ledger.calls[-1]['from'] == BASE_US - 300_000_000


def test_manual_refresh_invalidates_due_and_stale_age_is_explicit(tmp_path):
    clock = Clock()
    ledger = Ledger([fill(1, BASE_US - 1_000_000)])
    stats = service(tmp_path, ledger, clock)
    stats.sync_step({'id': 'account-a'})
    clock.advance(10)
    stats.sync_step({'id': 'account-a'})
    assert len(ledger.calls) == 1
    stats.request_sync('account-a')
    stats.sync_step({'id': 'account-a'})
    assert len(ledger.calls) == 2
    clock.advance(181)
    assert stats.snapshot('account-a')['stale'] is True


def test_network_failure_retains_verified_old_values_with_error_and_timestamp(tmp_path):
    clock = Clock()
    ledger = Ledger([fill(1, BASE_US - 1_000_000)])
    stats = service(tmp_path, ledger, clock)
    known = stats.sync_step({'id': 'account-a'})
    clock.advance(61)
    ledger.failure = RuntimeError('temporary upstream outage')
    result = stats.sync_step({'id': 'account-a'})
    assert result['status'] == 'error'
    assert result['turnover'] == known['turnover']
    assert result['updatedAt'] == known['updatedAt']
    assert result['toTime'] == known['toTime']
    assert result['complete'] is True  # Complete only through the displayed old cutoff.
    assert result['error'] == 'temporary upstream outage'


def test_unknown_pnl_and_liquidation_leave_verified_turnover_and_fees_visible(tmp_path):
    clock = Clock()
    records = [fill(1, BASE_US - 2_000_000, pnl=None),
               fill(2, BASE_US - 1_000_000, fee='2', pnl='0',
                    liquidation={'method': 'LIQUIDATION', 'liquidatedUser': 'not-persisted'})]
    stats = service(tmp_path, Ledger(records), clock)
    result = stats.sync_step({'id': 'account-a'})
    assert result['status'] == 'partial'
    assert result['historyComplete'] is True
    assert result['complete'] is True
    assert result['turnoverKnown'] is True
    assert result['feesKnown'] is True
    assert result['turnover'] == '200'
    assert result['fees'] == '2.1'
    assert result['pnlKnown'] is False
    assert result['realizedPnl'] is result['netRealizedPnl'] is result['grossRealizedPnl'] is None


def test_two_accounts_and_subaccounts_are_isolated_and_cache_never_contains_secrets(tmp_path):
    clock = Clock()
    ledgers = {'account-a': Ledger([fill(1, BASE_US - 1_000_000, accountIndex=0)], 0, 'wallet-alpha'),
               'account-b': Ledger([fill(1, BASE_US - 1_000_000, size='2', price='120', accountIndex=1)], 1, 'wallet-beta')}
    stats = AccountStatisticsService(tmp_path, lambda account: ledgers[account['id']],
                                    clock=clock, request_interval=0)
    first = stats.sync_step({'id': 'account-a'})
    second = stats.sync_step({'id': 'account-b'})
    assert first['turnover'] == '100'
    assert second['turnover'] == '240'
    assert stats.snapshot('account-a')['turnover'] == '100'
    contents = '\n'.join(path.read_text(encoding='utf-8') for path in tmp_path.glob('*.json'))
    assert 'private-fixture-do-not-persist' not in contents
    assert 'wallet-alpha' not in contents
    assert 'wallet-beta' not in contents
    stats.forget('account-a')
    assert len(list(tmp_path.glob('*.json'))) == 1
    assert stats.snapshot('account-b')['turnover'] == '240'


def test_scope_change_cannot_inherit_old_wallet_statistics(tmp_path):
    clock = Clock()
    ledger = Ledger([fill(1, BASE_US - 1_000_000)], address='old-scope')
    stats = service(tmp_path, ledger, clock)
    assert stats.sync_step({'id': 'account-a'})['turnover'] == '100'
    ledger.address = 'new-scope'
    ledger.records = [fill(2, BASE_US - 1_000_000, size='3')]
    stats.request_sync('account-a')
    result = stats.sync_step({'id': 'account-a'})
    assert result['turnover'] == '300'
    assert result['fillCount'] == 1


def test_wrong_subaccount_row_and_bad_microsecond_time_never_publish_totals(tmp_path):
    clock = Clock()
    ledger = Ledger([fill(1, BASE_US - 1_000_000, accountIndex=1)], index=0)
    stats = service(tmp_path, ledger, clock)
    result = stats.sync_step({'id': 'account-a'})
    assert result['status'] == 'error'
    assert result['turnover'] is None
    assert result['complete'] is False
    assert '子账户' in result['error']


def test_global_request_spacing_and_oldest_due_fairness(tmp_path):
    clock = Clock()
    ledgers = {'a': Ledger([fill(index, BASE_US - index * 1_000_000) for index in range(1, 6)]),
               'b': Ledger([])}
    stats = AccountStatisticsService(tmp_path, lambda account: ledgers[account['id']],
                                    clock=clock, page_size=3, request_interval=5)
    stats.snapshot('a')
    stats.snapshot('b')
    stats.sync_step({'id': 'a'})
    assert len(ledgers['a'].calls) == 1
    stats.sync_step({'id': 'b'})
    assert len(ledgers['b'].calls) == 0
    clock.advance(5)
    stats.sync_step({'id': 'a'})
    assert len(ledgers['a'].calls) == 1  # Pending b's older due time wins.
    result = stats.sync_step({'id': 'b'})
    assert result['status'] == 'ready'
    assert len(ledgers['b'].calls) == 1
