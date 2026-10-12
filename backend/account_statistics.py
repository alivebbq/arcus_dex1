"""Private, account-scoped aggregates of Arcus's persisted fill ledger.

Run sync_step in a collector thread separate from trading. snapshot never
performs HTTP. Every page uses inclusive microsecond bounds and a composite
fill identity; a saturated timestamp boundary is reported partial, never skipped.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable


HISTORY_FLOOR_US = 100_000_000_000_000  # Earliest timestamp the official API accepts.
REFRESH_INTERVAL = 60
STALE_AFTER = 180
PAGE_SIZE = 500
REQUEST_INTERVAL = 5
OVERLAP_SECONDS = 300
PNL_BASIS = 'Arcus fills.closedPnl，已扣交易手续费，不含资金费；强平口径不完整时不推算'


class StatisticsError(ValueError):
    pass


def _decimal(value: Any, label: str) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (ValueError, TypeError, InvalidOperation):
        raise StatisticsError(f'成交记录缺少有效{label}') from None


def _string(value: Decimal) -> str:
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _sanitize(error: Exception | str) -> str:
    return re.sub(r'(?i)(?:0x)?[0-9a-f]{64,}', '[redacted]', str(error))[:300]


def _identity(fill: dict[str, Any]) -> str:
    return json.dumps([str(fill['tradeId']), str(fill['orderId'])], separators=(',', ':'))


def aggregate_fills(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate source records exactly. Unknown PnL does not hide volume/fees."""
    unique = {_identity(fill): fill for fill in fills}
    turnover = fees = net = gross = Decimal(0)
    pnl_known = True
    liquidation_count = 0
    timestamps = []
    with localcontext() as context:
        context.prec = 80
        for fill in unique.values():
            size = _decimal(fill.get('size'), '成交数量')
            price = _decimal(fill.get('price'), '成交价格')
            fee = _decimal(fill.get('fee'), '手续费')
            if price <= 0 or size == 0:
                raise StatisticsError('成交价格和绝对成交数量必须大于 0')
            turnover += abs(size * price)
            fees += fee  # Negative maker rebates remain negative.
            timestamps.append(int(fill['createdAt']))
            liquidation = fill.get('liquidation') or {}
            if liquidation.get('method') == 'LIQUIDATION':
                # Docs: fee may include a penalty not deducted from closedPnl;
                # older liquidation closedPnl=0 was not backfilled. No guessed fix.
                liquidation_count += 1
                pnl_known = False
            if fill.get('closedPnl') is None:
                pnl_known = False
            else:
                closed = _decimal(fill['closedPnl'], '已实现盈亏')
                net += closed
                gross += closed + fee
    return {'turnover': _string(turnover), 'fees': _string(fees),
            'realizedPnl': _string(net) if pnl_known else None,
            'netRealizedPnl': _string(net) if pnl_known else None,
            'grossRealizedPnl': _string(gross) if pnl_known else None,
            'fillCount': len(unique), 'fromTime': min(timestamps) if timestamps else None,
            'turnoverKnown': True, 'feesKnown': True, 'pnlKnown': pnl_known,
            'liquidationFillCount': liquidation_count, 'pnlBasis': PNL_BASIS,
            'feeBasis': '成交记录 fee 汇总，负数为返佣；强平记录可能包含惩罚金',
            'pnlError': None if pnl_known else '已实现盈亏字段缺失或存在无法完整核实的强平口径'}


class AccountStatisticsService:
    def __init__(self, directory: Path | str, client_factory: Callable,
                 *, refresh_interval: float = REFRESH_INTERVAL,
                 stale_after: float = STALE_AFTER, page_size: int = PAGE_SIZE,
                 request_interval: float = REQUEST_INTERVAL,
                 overlap_seconds: int = OVERLAP_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass
        if not 1 <= page_size <= 1000:
            raise ValueError('page_size must be in 1..1000')
        self.client_factory = client_factory
        self.refresh_interval = refresh_interval
        self.stale_after = stale_after
        self.page_size = page_size
        self.request_interval = request_interval
        self.overlap_seconds = overlap_seconds
        self.clock = clock
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._account_locks: dict[str, threading.Lock] = {}
        self._forgotten: set[str] = set()
        self._next_request_at = 0.0

    def _path(self, account_id: str) -> Path:
        # No raw address, credentials, account name or caller-controlled path.
        filename = hashlib.sha256(str(account_id).encode()).hexdigest() + '.json'
        return self.directory / filename

    @staticmethod
    def _new(account_id: str) -> dict[str, Any]:
        return {'schema': 1, 'accountId': account_id, 'scope': None, 'records': {},
                'phase': 'backfill', 'scanToUs': None, 'cursorUs': None,
                'scanFromUs': HISTORY_FLOOR_US, 'coverageToUs': None,
                'published': None, 'status': 'syncing', 'error': None,
                'nextDue': 0.0, 'resyncRequested': False}

    def _state(self, account_id: str) -> dict[str, Any]:
        if account_id not in self._states:
            state = self._new(account_id)
            path = self._path(account_id)
            if path.exists():
                try:
                    saved = json.loads(path.read_text(encoding='utf-8'))
                    if (saved.get('schema') != 1 or saved.get('accountId') != account_id
                            or not isinstance(saved.get('records'), dict)
                            or (saved.get('published') is not None and not isinstance(saved['published'], dict))):
                        raise StatisticsError('账户统计缓存格式不匹配')
                    state = {**state, **saved}
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    state['status'], state['error'] = 'error', _sanitize(exc)
            self._states[account_id] = state
        return self._states[account_id]

    def _write(self, state: dict[str, Any]) -> None:
        if state['accountId'] in self._forgotten:
            return
        target = self._path(state['accountId'])
        temporary = target.with_suffix('.json.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass

    def snapshot(self, account_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._state(str(account_id))
            published = deepcopy(state.get('published'))
            if published is None:
                published = {'turnover': None, 'realizedPnl': None, 'fees': None,
                             'grossRealizedPnl': None, 'netRealizedPnl': None, 'fillCount': None,
                             'updatedAt': None, 'fromTime': None, 'toTime': None,
                             'turnoverKnown': False, 'feesKnown': False, 'pnlKnown': False,
                             'complete': False, 'historyComplete': False, 'pnlBasis': PNL_BASIS}
            updated = published.get('updatedAt')
            coverage = published.get('toTime')
            expired = (updated is None or self.clock() * 1000 - updated > self.stale_after * 1000
                       or coverage is None or self.clock() - coverage / 1_000_000 > self.stale_after)
            published.update(status=state['status'], error=state.get('error'),
                             syncedFillCount=len(state['records']),
                             stale=state.get('published') is not None and expired,
                             syncing=state.get('phase') in ('backfill', 'incremental'),
                             scope='全部市场、该交易所子账户的实际成交；含手工和其他程序，不含模拟交易',
                             historyScope='API 可用历史成交，包含本面板已持久保存的历史记录',
                             queriedFromUs=HISTORY_FLOOR_US)
            return published

    def request_sync(self, account_id: str) -> None:
        with self._lock:
            if str(account_id) in self._forgotten:
                return
            state = self._state(str(account_id))
            state['nextDue'] = 0.0
            state['resyncRequested'] = True
            if state['status'] in ('partial', 'error') and state['published'] is None:
                state['phase'] = 'backfill'

    def forget(self, account_id: str) -> None:
        with self._lock:
            account_id = str(account_id)
            self._forgotten.add(account_id)
            self._states.pop(account_id, None)
            for path in (self._path(account_id), self._path(account_id).with_suffix('.json.tmp')):
                path.unlink(missing_ok=True)

    @staticmethod
    def _scope(client: Any) -> str:
        value = [getattr(client, 'environment', 'mainnet'), getattr(client, 'address', ''),
                 getattr(client, 'account_index', 0)]
        return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _row(fill: dict[str, Any], lower_us: int, upper_us: int, client: Any) -> dict[str, Any]:
        if not isinstance(fill, dict) or any(fill.get(field) is None for field in
                ('tradeId', 'orderId', 'size', 'price', 'fee', 'createdAt')):
            raise StatisticsError('成交账本缺少成交身份、金额或时间字段')
        stamp = fill['createdAt']
        if isinstance(stamp, bool) or not isinstance(stamp, int) or not lower_us <= stamp <= upper_us:
            raise StatisticsError('成交时间不是本次查询范围内的 Unix 微秒')
        if fill.get('accountIndex') is not None and fill['accountIndex'] != getattr(client, 'account_index', 0):
            raise StatisticsError('成交记录子账户与请求范围不一致')
        address = getattr(client, 'address', '')
        if address and fill.get('address') and str(fill['address']).lower() != str(address).lower():
            raise StatisticsError('成交记录钱包与请求范围不一致')
        row = {key: fill[key] for key in ('tradeId', 'orderId', 'size', 'price', 'fee', 'createdAt')}
        for key in ('marketId', 'marketDisplayName', 'closedPnl', 'side', 'role'):
            if key in fill:
                row[key] = fill[key]
        if fill.get('liquidation'):
            if (not isinstance(fill['liquidation'], dict)
                    or fill['liquidation'].get('method') not in ('LIQUIDATION', 'ADL')):
                raise StatisticsError('成交记录的强平类型未明确，不能猜测盈亏口径')
            row['liquidation'] = {'method': fill['liquidation'].get('method')}
        # Validate money before committing a pagination cursor.
        aggregate_fills([row])
        return row

    def sync_step(self, account: dict[str, Any], max_pages: int = 1) -> dict[str, Any]:
        """Fetch bounded pages. Use from a dedicated collector, outside trading locks."""
        account_id = str(account['id'])
        with self._lock:
            if account_id in self._forgotten:
                return {'status': 'error', 'complete': False, 'error': '账户已移除'}
            mutex = self._account_locks.setdefault(account_id, threading.Lock())
        if not mutex.acquire(blocking=False):
            return self.snapshot(account_id)
        try:
            for _ in range(max(0, int(max_pages))):
                with self._lock:
                    state = self._state(account_id)
                    now = self.clock()
                    due = [(value.get('nextDue', 0), identity) for identity, value in self._states.items()
                           if identity not in self._forgotten and value.get('nextDue', 0) <= now]
                    chosen = min(due)[1] if due else None
                    if (state.get('nextDue', 0) > now or now < self._next_request_at
                            or (chosen is not None and chosen != account_id)):
                        break
                    self._next_request_at = now + self.request_interval
                    state['resyncRequested'] = False
                    state['status'] = 'syncing'
                    if state['phase'] == 'idle':
                        state.update(phase='incremental', scanToUs=int(now * 1_000_000), cursorUs=int(now * 1_000_000),
                                     scanFromUs=max(HISTORY_FLOOR_US, int(state['coverageToUs']) - self.overlap_seconds * 1_000_000))
                    elif state.get('scanToUs') is None:
                        state.update(scanToUs=int(now * 1_000_000), cursorUs=int(now * 1_000_000))
                    lower, upper = int(state['scanFromUs']), int(state['cursorUs'])
                try:
                    client = self.client_factory(account)
                    scope = self._scope(client)
                    with self._lock:
                        if state.get('scope') is not None and state['scope'] != scope:
                            # A changed wallet/subaccount cannot inherit old statistics.
                            state.clear()
                            state.update(self._new(account_id), scope=scope,
                                         scanToUs=int(now * 1_000_000), cursorUs=int(now * 1_000_000))
                            lower, upper = HISTORY_FLOOR_US, int(state['cursorUs'])
                        else:
                            state['scope'] = scope
                    page = client.fills(None, limit=self.page_size, from_us=lower, to_us=upper)
                    if not isinstance(page, list) or len(page) > self.page_size:
                        raise StatisticsError('成交账本分页格式不正确')
                    rows = [self._row(fill, lower, upper, client) for fill in page]
                    with self._lock:
                        if account_id in self._forgotten:
                            break
                        for row in rows:
                            state['records'][_identity(row)] = row
                        state['error'] = None
                        if len(page) < self.page_size:
                            summary = aggregate_fills(list(state['records'].values()))
                            summary.update(updatedAt=int(self.clock() * 1000), toTime=state['scanToUs'],
                                           complete=True, historyComplete=True)
                            state.update(published=summary, coverageToUs=state['scanToUs'], phase='idle',
                                         status='ready' if summary['pnlKnown'] else 'partial',
                                         nextDue=self.clock() + self.refresh_interval)
                            if self.clock() - state['scanToUs'] / 1_000_000 > self.refresh_interval:
                                # A long backfill has a fixed old cutoff; catch up
                                # immediately instead of treating its completion as fresh.
                                state['nextDue'] = self.clock()
                        else:
                            oldest = min(row['createdAt'] for row in rows)
                            if oldest == upper:
                                state.update(status='partial', error='同一微秒的成交达到 API 分页上限，不能确认完整统计',
                                             nextDue=self.clock() + self.refresh_interval)
                            else:
                                state.update(cursorUs=oldest, nextDue=self.clock() + 1)
                        if state.get('resyncRequested'):
                            state['nextDue'] = 0.0
                        self._write(state)
                except Exception as exc:
                    with self._lock:
                        state.update(status='error', error=_sanitize(exc),
                                     nextDue=self.clock() + max(self.refresh_interval, getattr(exc, 'retry_after_ms', 0) / 1000))
                        try:
                            self._write(state)
                        except OSError:
                            pass
                    break
            return self.snapshot(account_id)
        finally:
            mutex.release()
