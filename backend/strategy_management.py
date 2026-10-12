"""Read-only deletion guards. Caller must hold the control-plane lock throughout."""
from decimal import Decimal, InvalidOperation
from math import isfinite
import time

TERMINAL = {'FILLED', 'CANCELED', 'CANCELLED', 'MARGIN_CANCELED', 'REJECTED', 'EXPIRED', 'TPSL_CANCELED'}
DELETABLE = {'draft', 'paused', 'stopped'}


class StrategyDeletionBlocked(ValueError):
    status_code = 409


def _number(value, label):
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError()
        return number
    except (ValueError, TypeError, InvalidOperation):
        raise StrategyDeletionBlocked(label + '尚未确认，不能删除策略') from None


def _orders(strategy):
    current = []
    for slot in strategy.get('slots', []):
        current.extend(order for order in (slot.get('entry'), slot.get('exit')) if order)
    current.extend(order for order in strategy.get('orders', []) if order)
    current.extend(order for order in (strategy.get('protection'), strategy.get('emergency')) if order)
    merged = {}
    for order in current:
        identity = order.get('clientId') or order.get('orderId')
        if not identity:
            raise StrategyDeletionBlocked('策略有缺少订单标识的记录，不能删除')
        if identity in merged:
            prior = merged[identity]
            if prior.get('orderId') and order.get('orderId') and prior['orderId'] != order['orderId']:
                raise StrategyDeletionBlocked('同一订单标识存在冲突，不能删除策略')
            order = {**prior, **order}
        merged[identity] = {**order, 'historicalOnly': False}
    for client_id, order in strategy.get('orderRegistry', {}).items():
        if client_id not in merged:
            merged[client_id] = {**order, 'clientId': client_id, 'historicalOnly': True}
    return list(merged.values())


def _accounted(order):
    if order.get('accountedFill') is not None:
        return _number(order['accountedFill'], '已核算成交数量')
    if order.get('filledSize') is not None:
        filled = _number(order['filledSize'], '最终成交数量')
        if order.get('historicalOnly') or filled == 0:
            return filled
    raise StrategyDeletionBlocked('订单缺少最终已核算成交数量，不能删除策略')


def _local_guard(strategy):
    if strategy.get('status') not in DELETABLE:
        raise StrategyDeletionBlocked('请先暂停或停止策略，完成挂单与持仓对账后再删除')
    if _number(strategy.get('inventory', '0'), '策略库存') or _number(strategy.get('residualQuantity', '0'), '剩余持仓'):
        raise StrategyDeletionBlocked('策略仍有持仓，删除不会代替平仓，请先处理持仓')
    if strategy.get('position'):
        raise StrategyDeletionBlocked('策略仍有持仓记录，等待平仓对账完成后再删除')
    if strategy.get('trade'):
        raise StrategyDeletionBlocked('趋势交易或保护单清理尚未结束，不能删除策略')
    setup = strategy.get('marginSetup') or {}
    if setup.get('state') == 'pending' or setup.get('uncertain') or setup.get('pending'):
        raise StrategyDeletionBlocked('杠杆或保证金设置尚未确认，不能删除策略')
    for slot in strategy.get('slots', []):
        if _number(slot.get('inventory', '0'), '网格库存') or _number(slot.get('entryCost', '0'), '未结算持仓成本'):
            raise StrategyDeletionBlocked('策略库存或成交成本尚未结清，不能删除')
    orders = _orders(strategy)
    for order in orders:
        accounted = _accounted(order)
        if order.get('observedFilledSize') is not None and _number(order['observedFilledSize'], '已观察成交数量') != accounted:
            raise StrategyDeletionBlocked('存在尚未核算的成交数量，不能删除策略')
        if order.get('filledSize') is not None and _number(order['filledSize'], '成交数量') != accounted:
            raise StrategyDeletionBlocked('订单成交数量尚未完成核算，不能删除策略')
    return orders


def delete_strategy(store, strategy, account, client_factory):
    """Delete a safely settled record, without canceling orders or closing positions.

    Foreign positions never prove ownership and never block a settled old record.
    A fresh complete open-order snapshot must exclude every owned ID/prefix.
    Current ambiguous records require authoritative terminal fill evidence. Fully
    accounted terminal history can rely on its durable evidence after retention.
    """
    if strategy.get('mode') not in ('paper', 'live'):
        raise StrategyDeletionBlocked('策略执行模式尚未确认，不能删除')
    if strategy.get('status') not in DELETABLE:
        raise StrategyDeletionBlocked('请先暂停或停止策略后再删除')
    if account is not None and account.get('id') != strategy.get('accountId'):
        raise StrategyDeletionBlocked('账户与策略不匹配，不能删除')
    if strategy['mode'] == 'paper':
        # Virtual inventory/protection cannot create exchange exposure. Discarding
        # a stopped simulation needs neither reconciliation nor account writes.
        deleted = store.delete_strategy(strategy['id'])
        if deleted:
            store.event('info', '已删除模拟策略及其虚拟订单/持仓：' + str(strategy.get('name', '策略')),
                        strategy['id'], accountId=strategy.get('accountId'))
        return {'deleted': bool(deleted), 'id': strategy['id']}
    orders = _local_guard(strategy)
    live = strategy.get('mode') == 'live'
    previously_used = bool(strategy.get('hasStarted') or strategy.get('activatedAt') or strategy.get('startedAt') or orders)
    if live and previously_used:
        if account is None:
            raise StrategyDeletionBlocked('原实盘账户已移除，无法核验交易所订单，不能删除策略')
        try:
            client = client_factory(account)
            snapshot = client.account_snapshot()
            stamp = snapshot.get('updatedAt', snapshot.get('updatedAtMs', 0))
            now = int(time.time() * 1000)
            if (snapshot.get('connected') is not True or not isinstance(stamp, (int, float))
                    or not isfinite(stamp) or abs(now - stamp) > 45000):
                raise StrategyDeletionBlocked('交易所账户状态未连接或已过期，不能删除策略')
            opened = snapshot.get('orders', snapshot.get('openOrders'))
            if (not isinstance(opened, list) or len(opened) >= 1000
                    or any(not isinstance(order, dict) or not (order.get('orderId') or order.get('clientId'))
                           for order in opened)):
                raise StrategyDeletionBlocked('交易所挂单快照不完整，不能删除策略')
            client_ids = {order['clientId'] for order in orders if order.get('clientId')}
            order_ids = {str(order['orderId']) for order in orders if order.get('orderId')}
            prefix = f"ag-{strategy['id'][:12]}-"
            if any(order.get('clientId') in client_ids or str(order.get('orderId')) in order_ids
                   or str(order.get('clientId') or '').startswith(prefix) for order in opened):
                raise StrategyDeletionBlocked('交易所仍有本策略挂单，完成撤单对账后再删除')
            market = {'marketId': strategy.get('marketId'), 'symbol': strategy.get('market'),
                      'marketDisplayName': strategy.get('market')}
            for order in orders:
                definitive = (str(order.get('status', '')).upper() in TERMINAL
                              and order.get('filledSize') is not None
                              and (order.get('state') == 'terminal'
                                   or (order.get('state') is None and not order.get('uncertain'))))
                if definitive:
                    continue
                row = client.order_status(market, order.get('clientId', ''), order.get('orderId'))
                status = str(row.get('status', 'UNKNOWN')).upper()
                if order.get('historicalOnly') and status == 'UNKNOWN':
                    # Compact historical registry rows are absent from all current
                    # order frames; their completed fill quantity is durable, and
                    # the fresh full snapshot proves no owned order is still live.
                    if order.get('filledSize') is not None:
                        continue
                    raise StrategyDeletionBlocked('历史订单缺少最终成交数量，不能删除策略')
                if status not in TERMINAL or row.get('filledSize') is None:
                    raise StrategyDeletionBlocked('本策略订单终态尚未确认，不能删除')
                if _number(row['filledSize'], '交易所最终成交数量') != _accounted(order):
                    raise StrategyDeletionBlocked('交易所新增成交尚未核算，不能删除策略')
        except StrategyDeletionBlocked:
            raise
        except Exception:
            raise StrategyDeletionBlocked('交易所订单核验失败，保留策略记录，请恢复连接后重试') from None
    else:
        if any(str(order.get('status', '')).upper() not in TERMINAL
               or order.get('state') not in (None, 'terminal') for order in orders if not order.get('historicalOnly')):
            raise StrategyDeletionBlocked('策略仍有未确认挂单，不能删除')
    deleted = store.delete_strategy(strategy['id'])
    if deleted:
        store.event('info', '已删除已结清策略：' + str(strategy.get('name', '策略')),
                    strategy['id'], accountId=strategy.get('accountId'))
    return {'deleted': bool(deleted), 'id': strategy['id']}
