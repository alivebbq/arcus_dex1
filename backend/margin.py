"""Perpetual margin sizing. No borrowing/spot-margin endpoints are involved."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING

RESERVE_FRACTION = Decimal('0.05')


def number(value, label):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError(label + '必须是有限数字') from None


def budget_type(config):
    # Existing persisted records have no budgetType; their quantities stay unchanged.
    return config.get('budgetType', 'notional' if 'id' in config else 'margin')


def active_margin_fraction(market):
    if market.get('marginStateKnown') is False:
        raise ValueError('市场交易时段或保证金要求尚未确认')
    base = number(market.get('initialMarginFraction'), '市场初始保证金率')
    if base <= 0 or base > 1:
        raise ValueError('市场初始保证金率无效')
    if market.get('regularTradingHours') and market.get('isOutsideRth') is None:
        raise ValueError('市场交易时段尚未确认，不能计算合约保证金')
    if market.get('isOutsideRth'):
        offhours = number(market.get('offHoursInitialMarginFraction'), '非交易时段保证金率')
        if offhours <= 0 or offhours > 1:
            raise ValueError('非交易时段保证金率无效')
        base = max(base, offhours)
    return base


def margin_sizing(config, market, levels, entries, step):
    budget = number(config['budget'], '投入保证金')
    requested = number(config.get('leverage', 1), '合约杠杆')
    if requested != requested.to_integral_value() or requested < 1:
        raise ValueError('合约杠杆必须是大于等于 1 的整数')
    fraction = active_margin_fraction(market)
    base_fraction = number(market['initialMarginFraction'], '市场基础保证金率')
    cap = min(1000, int((1 / base_fraction).to_integral_value(rounding=ROUND_FLOOR)))
    if requested > cap:
        raise ValueError(f'所选合约杠杆超过当前市场上限 {cap} 倍')
    mode = str(config.get('marginMode', 'cross')).lower()
    if mode not in ('cross', 'isolated'):
        raise ValueError('保证金模式必须是 cross 或 isolated')
    effective_fraction = max(1 / requested, fraction)
    mark = number(market.get('markPrice', levels[-1]), '标记价格')
    valuation = max(levels[-1], mark)
    reserve = budget * RESERVE_FRACTION
    nominal_cap = budget * requested
    per_quantity_imr = Decimal(len(entries)) * valuation * effective_fraction
    nominal_quantity = nominal_cap / sum(entries)
    margin_quantity = (budget - reserve) / per_quantity_imr
    quantity = (min(nominal_quantity, margin_quantity) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    minimum_qty = max(number(market.get('minOrderSize', step), '最小数量'),
                      number(market.get('minOrderNotional', '0'), '最小名义金额') / min(entries))
    minimum_qty = (minimum_qty / step).to_integral_value(rounding=ROUND_CEILING) * step
    minimum_nominal = minimum_qty * sum(entries)
    minimum_budget = max(minimum_nominal / requested,
                         minimum_qty * per_quantity_imr / (1 - RESERVE_FRACTION))
    imr = quantity * per_quantity_imr
    return quantity, {k: format(v, 'f') if isinstance(v, Decimal) else v for k, v in {
        'budgetType': 'margin', 'allocatedMargin': budget, 'leverage': int(requested), 'marginMode': mode,
        'notionalBudget': nominal_cap, 'estimatedInitialMargin': imr, 'marginReserve': reserve,
        'requiredCollateral': imr + reserve, 'valuationPrice': valuation,
        'effectiveInitialMarginFraction': effective_fraction, 'reserveFraction': RESERVE_FRACTION,
        'minMarginBudget': minimum_budget, 'minNotionalBudget': minimum_nominal, 'maxLeverage': cap,
    }.items()}
