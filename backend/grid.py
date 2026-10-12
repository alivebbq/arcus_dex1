"""Durable, maker-only grid execution.

Exchange acknowledgements are never treated as fills.  Each client ID is saved
before submission, and an ambiguous submission is only reconciled, never sent
again.  REST's authoritative filledSize is accounted cumulatively.  A partially
filled entry is canceled before its filled quantity can become an exit.

Market rules follow docs/arcus/markets.md; in particular tick-tier upper bounds
are exclusive and position-opening minOrderNotional does not apply to exits.
The control plane must serialize calls and supply fresh market/account data.
"""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import re
import time
import uuid
from .margin import budget_type, margin_sizing, active_margin_fraction


ZERO = Decimal("0")
TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "MARGIN_CANCELED", "REJECTED", "EXPIRED"}
FORCED = {"LIQUIDATED", "ADL"}
OPEN = {"OPEN", "PARTIALLY_FILLED", "PARTIAL", "NEW"}


def decimal(value, label="数值"):
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label}必须是有限数字") from None


def string(number):
    return format(number, "f")


def safe_error(error):
    return re.sub(r"(?i)(?:0x)?[0-9a-f]{64,}", "[redacted]", str(error))[:400]


def now_ms():
    return int(time.time() * 1000)


def tick_for(price, market):
    tiers = market.get("tickTiers") or []
    if tiers:
        for tier in tiers:
            bound = tier.get("upToPrice")
            if bound is None or price < decimal(bound, "价格档位上限"):
                tick = decimal(tier.get("tick"), "价格档位步长")
                if tick <= 0:
                    raise ValueError("价格档位步长必须大于 0")
                return tick
        raise ValueError("市场价格档位缺少无上限档位")
    tick = decimal(market.get("tickSize"), "市场价格步长")
    if tick <= 0:
        raise ValueError("市场价格步长必须大于 0")
    return tick


def quantize_price(price, market, rounding=ROUND_FLOOR):
    tiers = market.get("tickTiers") or []
    if not tiers:
        tick = tick_for(price, market)
        return (price / tick).to_integral_value(rounding=rounding) * tick
    # Enumerating bands also handles non-divisible adjacent ticks, where simply
    # re-rounding could oscillate across an exclusive upper boundary.
    lower, candidates = ZERO, []
    for index, tier in enumerate(tiers):
        tick = decimal(tier.get("tick"), "价格档位步长")
        upper = decimal(tier["upToPrice"], "价格档位上限") if tier.get("upToPrice") is not None else None
        if tick <= 0 or (upper is not None and upper <= lower) or (upper is None and index != len(tiers) - 1):
            raise ValueError("市场价格档位无效")
        if rounding == ROUND_FLOOR:
            candidate = (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            if upper is not None:
                candidate = min(candidate, ((upper / tick).to_integral_value(rounding=ROUND_CEILING) - 1) * tick)
            if candidate >= lower and candidate <= price:
                candidates.append(candidate)
        else:
            candidate = (max(price, lower) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            if candidate >= price and (upper is None or candidate < upper):
                candidates.append(candidate)
        if upper is not None:
            lower = upper
    if tiers[-1].get("upToPrice") is not None:
        raise ValueError("市场价格档位缺少无上限档位")
    if not candidates:
        raise ValueError("价格档位边界无法对齐")
    return max(candidates) if rounding == ROUND_FLOOR else min(candidates)


class GridEngine:
    def __init__(self, store, client_factory):
        self.store = store
        self.client_factory = client_factory

    def preview(self, config, market):
        result = {"levels": [], "quantity": "0", "orderNotional": "0",
                  "maxOrderNotional": "0", "totalNotional": "0",
                  "estimatedCycleProfit": "0", "cycleProfits": [],
                  "profitBasis": "gross_before_fees_and_funding", "errors": []}
        try:
            lower = decimal(config.get("lower", config.get("lowerPrice")), "区间下限")
            upper = decimal(config.get("upper", config.get("upperPrice")), "区间上限")
            budget = decimal(config.get("budget"), "投入额度")
            count_raw = decimal(config.get("gridCount"), "网格数量")
            if lower <= 0 or upper <= lower:
                raise ValueError("区间必须满足 0 < 下限 < 上限")
            if count_raw != count_raw.to_integral_value() or not 2 <= count_raw <= 50:
                raise ValueError("网格数量必须是 2 到 50 的整数")
            if budget <= 0 or budget > 1000000:
                raise ValueError("投入额度必须大于 0 且不超过 1,000,000")
            if config.get("direction", "long") not in ("long", "short"):
                raise ValueError("网格方向必须是 long 或 short")
            if config.get("mode", "paper") not in ("paper", "live"):
                raise ValueError("执行模式必须是 paper 或 live")
            if str(market.get("status", "ONLINE")).upper() != "ONLINE":
                raise ValueError("市场未上线，不能创建交易网格")
            step = decimal(market.get("stepSize"), "市场数量步长")
            if step <= 0:
                raise ValueError("市场数量步长必须大于 0")
            count = int(count_raw)
            levels = [quantize_price(lower + (upper - lower) * i / count, market,
                                     ROUND_CEILING if i == 0 else ROUND_FLOOR)
                      for i in range(count + 1)]
            if levels[0] < lower or levels[-1] > upper or any(
                    b <= a for a, b in zip(levels, levels[1:])):
                raise ValueError("区间过窄，按市场价格步长对齐后网格价格重复")
            entries = levels[:-1] if config.get("direction", "long") == "long" else levels[1:]
            if budget_type(config) == 'margin':
                quantity, margin = margin_sizing(config, market, levels, entries, step)
                result.update(margin)
            else:
                quantity = (budget / sum(entries) / step).to_integral_value(rounding=ROUND_FLOOR) * step
                result.update(budgetType='notional', notionalBudget=string(budget),
                              requiredCollateral=string(budget), allocatedMargin=None,
                              leverage=None, marginMode=None)
            min_size = decimal(market.get("minOrderSize", step), "最小订单数量")
            min_notional = decimal(market.get("minOrderNotional", market.get("minNotional", "0")),
                                   "最小订单金额")
            if quantity <= 0 or quantity < min_size:
                raise ValueError("投入额度不足以满足每格最小订单数量")
            if market.get("maxOrderSize") is not None and quantity > decimal(market["maxOrderSize"]):
                raise ValueError("每格数量超过市场最大订单数量")
            notionals = [quantity * price for price in entries]
            if min(notionals) < min_notional:
                raise ValueError(f"投入额度不足以满足每格最小订单金额 {string(min_notional)}")
            profits = [quantity * (b - a) for a, b in zip(levels, levels[1:])]
            result.update(levels=[string(p) for p in levels], quantity=string(quantity),
                          orderNotional=string(min(notionals)), maxOrderNotional=string(max(notionals)),
                          totalNotional=string(sum(notionals)), cycleProfits=[string(p) for p in profits],
                          estimatedCycleProfit=string(min(profits)))
        except ValueError as exc:
            result["errors"].append(str(exc))
        return result

    def create(self, config, market):
        preview = self.preview(config, market)
        if preview["errors"]:
            raise ValueError("；".join(preview["errors"]))
        strategy = {"id": uuid.uuid4().hex, "name": str(config.get("name", "网格策略"))[:80],
                    "accountId": config.get("accountId"),
                    "market": config.get("market", market.get("symbol", market.get("marketDisplayName"))),
                    "marketId": market.get("marketId"), "mode": config.get("mode", "paper"),
                    "direction": config.get("direction", "long"),
                    "lower": string(decimal(config.get("lower", config.get("lowerPrice")))),
                    "upper": string(decimal(config.get("upper", config.get("upperPrice")))),
                    "budget": string(decimal(config["budget"])), "gridCount": int(config["gridCount"]),
                    "quantity": preview["quantity"], "levels": preview["levels"],
                    "totalNotional": preview["totalNotional"], "status": "draft", "desiredStatus": "paused",
                    "createdAt": now_ms(), "updatedAt": now_ms(), "hasStarted": False,
                    "error": None, "suspended": False, "outOfRange": False,
                    "inventory": "0", "residualQuantity": "0", "orders": [],
                    "stats": {"cycles": 0, "filledOrders": 0, "realizedPnl": "0", "grossProfit": "0",
                              "fees": None, "profitBasis": "gross_before_fees_and_funding",
                              "estimatedFees": None}, "slots": []}
        for key in ('budgetType', 'allocatedMargin', 'leverage', 'marginMode', 'notionalBudget',
                    'estimatedInitialMargin', 'marginReserve', 'requiredCollateral',
                    'effectiveInitialMarginFraction', 'reserveFraction', 'valuationPrice'):
            if key in preview:
                strategy[key] = preview[key]
        for index, (lower, upper) in enumerate(zip(preview["levels"], preview["levels"][1:])):
            entry, exit_price = (lower, upper) if strategy["direction"] == "long" else (upper, lower)
            strategy["slots"].append({"index": index, "entryPrice": entry, "exitPrice": exit_price,
                                      "quantity": preview["quantity"], "cycle": 0, "state": "idle",
                                      "inventory": "0", "entry": None, "exit": None,
                                      "entryCost": "0", "retryAfter": 0, "error": None})
        self._save(strategy)
        return strategy

    def _save(self, strategy):
        inventory = sum((decimal(slot["inventory"]) for slot in strategy["slots"]), ZERO)
        # A confirmed fill lacking its official average remains unaccounted in
        # the cost ledger, but its known quantity must still be visible.
        observed_unaccounted = ZERO
        for slot in strategy["slots"]:
            for leg in ("entry", "exit"):
                order = slot.get(leg)
                if order and order.get("observedFilledSize") is not None:
                    delta = decimal(order["observedFilledSize"]) - decimal(order["accountedFill"])
                    observed_unaccounted += delta if leg == "entry" else -delta
        strategy["inventory"] = string(inventory)
        strategy["residualQuantity"] = string(inventory + observed_unaccounted)
        strategy["orders"] = [{"slot": slot["index"], "leg": leg, **order}
                              for slot in strategy["slots"] for leg in ("entry", "exit")
                              if (order := slot.get(leg)) and order["status"] not in TERMINAL]
        strategy["updatedAt"] = now_ms()
        self.store.save_strategy(strategy)

    def _event(self, strategy, level, message):
        self.store.event(level, message, strategy["id"])

    def _snapshot(self, account):
        return account.get("snapshot", account)

    def _matches_market(self, record, strategy):
        if record.get("marketId") is not None and strategy.get("marketId") is not None:
            return str(record["marketId"]) == str(strategy["marketId"])
        symbol = record.get("market", record.get("symbol", record.get("marketDisplayName")))
        return str(symbol).upper() == str(strategy["market"]).upper()

    def _position_size(self, position):
        size = decimal(position.get("size", position.get("quantity", "0")), "持仓数量")
        if str(position.get("side", "")).upper() in ("SHORT", "SELL") and size > 0:
            return -size
        return size

    def start(self, strategy, account, market):
        if strategy["status"] == "running":
            return strategy
        validation = self.preview(strategy, market)
        if validation["errors"]:
            raise ValueError("；".join(validation["errors"]))
        if budget_type(strategy) == 'margin' and decimal(strategy['quantity']) > decimal(validation['quantity']):
            raise ValueError('当前保证金要求已提高，原网格数量超出投入预算；请按新预估重新创建策略')
        if strategy["mode"] == "live":
            snapshot = self._snapshot(account)
            if not snapshot.get("connected") or now_ms() - snapshot.get("updatedAt", 0) > 45000:
                raise ValueError("账户状态未连接或超过 45 秒未同步")
            if not strategy.get("hasStarted"):
                free = decimal(snapshot.get("freeCollateral", snapshot.get("availableBalance")), "可用保证金")
                if free < decimal(strategy["budget"]):
                    raise ValueError('可用保证金不足以覆盖本策略投入' if budget_type(strategy) == 'margin'
                                     else '可用保证金不足；旧配置投入额度按名义本金计算')
            else:
                # Reconcile durable submissions before comparing the fresh account position.
                self._reconcile(strategy, self.client_factory(account), market)
                if self._unresolved(strategy):
                    self._save(strategy)
                    raise ValueError("已有订单状态尚未确认，请等待对账后恢复")
            own_ids = {order["clientId"] for slot in strategy["slots"]
                       for order in (slot.get("entry"), slot.get("exit")) if order}
            own_order_ids = {str(order["orderId"]) for slot in strategy["slots"]
                             for order in (slot.get("entry"), slot.get("exit")) if order and order.get("orderId")}
            for order in snapshot.get("orders", snapshot.get("openOrders", [])):
                if self._matches_market(order, strategy) and order.get("clientId") not in own_ids and str(
                        order.get("orderId")) not in own_order_ids:
                    raise ValueError("该市场有非本策略挂单，请先撤单或选择其他账户")
            actual = sum((self._position_size(p) for p in snapshot.get("positions", [])
                          if self._matches_market(p, strategy)), ZERO)
            expected = sum((decimal(slot["inventory"]) for slot in strategy["slots"]), ZERO)
            if strategy["direction"] == "short":
                expected = -expected
            if actual != expected:
                raise ValueError("该市场持仓与策略记录不一致；首次启动要求无持仓")
            if budget_type(strategy) == 'margin' and not self._ensure_margin(strategy, account, market, allow_write=True):
                self._save(strategy)
                return strategy
            if budget_type(strategy) == 'margin' and not self._refresh_margin_account(strategy, account, market):
                self._save(strategy)
                return strategy
        strategy.update(status="running", desiredStatus="running", suspended=False,
                        error=None, hasStarted=True, startedAt=now_ms())
        self._save(strategy)
        self._event(strategy, "info", f"{'模拟' if strategy['mode'] == 'paper' else '实盘'}网格已启动，全部订单使用挂单模式")
        return self.tick(strategy, account, market)

    def _ensure_margin(self, strategy, account, market, allow_write=False):
        """Persist config intent before writes; ACK/timeout only permits GET polling."""
        client = self.client_factory(account)
        setup = strategy.get('marginSetup', {})
        target_leverage = int(strategy['leverage'])
        target_mode = strategy['marginMode'].upper()
        configuring_write = False
        if setup.get('state') == 'error' and not allow_write and not strategy.get('hasStarted'):
            strategy['status'] = 'error'
            return False

        def matches(current):
            return (isinstance(current, dict) and decimal(current.get('leverage', '0')) == target_leverage
                    and str(current.get('marginMode')).upper() == target_mode)

        try:
            current = client.margin_config(market)
            if matches(current):
                strategy['marginSetup'] = {**setup, 'state': 'verified', 'verifiedAt': now_ms(),
                                           'current': current, 'error': None}
                return True
            # Existing inventory must never undergo an automatic leverage/mode change.
            if strategy.get('hasStarted'):
                self._fault(strategy, '交易所合约杠杆或保证金模式已改变，已暂停新开仓，请核对后恢复')
                return False
            if not allow_write or setup.get('state') == 'pending':
                strategy['status'] = 'configuring'
                return False
            setup = {'state': 'pending', 'attemptedAt': now_ms(), 'requestId': None, 'error': None}
            strategy.update(marginSetup=setup, status='configuring', desiredStatus='running')
            self._save(strategy)
            configuring_write = True
            response = client.configure_margin(market, target_leverage, strategy['marginMode'])
            setup['requestId'] = response.get('requestId')
            if response.get('verified') and matches(response.get('current')):
                setup.update(state='verified', verifiedAt=now_ms(), current=response['current'])
                self._save(strategy)
                return True
            self._event(strategy, 'info', '合约杠杆与保证金模式正在由交易所确认，确认前不会提交网格订单')
        except Exception as exc:
            error = safe_error(exc)
            explicit_rejection = configuring_write and not getattr(exc, 'uncertain', False)
            if not explicit_rejection and (getattr(exc, 'uncertain', False) or setup.get('state') == 'pending'):
                # A read error after a submitted intent cannot prove rejection.
                setup.update(state='pending', error=error)
                strategy.update(marginSetup=setup, status='configuring')
            else:
                setup.update(state='error', error=error)
                strategy.update(marginSetup=setup, status='error', error='合约设置未完成：' + error)
            self._save(strategy)
            return False
        self._save(strategy)
        return False

    def _refresh_margin_account(self, strategy, account, market):
        fresh = self.client_factory(account).account_snapshot()
        fresh['updatedAt'] = fresh.get('updatedAt', fresh.get('updatedAtMs', now_ms()))
        account['snapshot'] = fresh
        if hasattr(self.store, 'save_snapshot'):
            self.store.save_snapshot(account['id'], fresh)
        if not fresh.get('connected') or decimal(fresh.get('availableBalance', fresh.get('freeCollateral')), '可用保证金') < self.remaining_collateral(strategy, account, market):
            self._fault(strategy, '合约设置确认后可用保证金不足，已取消启动')
            return False
        if not strategy.get('hasStarted'):
            if any(self._matches_market(p, strategy) and self._position_size(p) != ZERO for p in fresh.get('positions', [])):
                self._fault(strategy, '等待合约设置期间出现已有持仓，已取消启动')
                return False
            if any(self._matches_market(o, strategy) for o in fresh.get('orders', fresh.get('openOrders', []))):
                self._fault(strategy, '等待合约设置期间出现其他挂单，已取消启动')
                return False
        return True

    def remaining_collateral(self, strategy, account, market):
        """freeCollateral already subtracts funded position IMR; reserve only the rest."""
        allocated = decimal(strategy['budget'])
        if budget_type(strategy) != 'margin' or not strategy.get('hasStarted'):
            return allocated
        fraction = max(Decimal(1) / int(strategy['leverage']), active_margin_fraction(market))
        posted_imr = ZERO
        for position in self._snapshot(account).get('positions', []):
            if not self._matches_market(position, strategy):
                continue
            mark = decimal(position.get('markPrice', position.get('markPx', '0')) or '0')
            if mark <= 0:
                mark = decimal(market.get('markPrice') or '0')
            if mark > 0:
                posted_imr += abs(self._position_size(position)) * mark * fraction
        return max(ZERO, allocated - posted_imr)

    def pause(self, strategy, account, market):
        return self._request_halt(strategy, account, market, "paused")

    def stop(self, strategy, account, market):
        return self._request_halt(strategy, account, market, "stopped")

    def _request_halt(self, strategy, account, market, desired):
        strategy.update(desiredStatus=desired, status="pausing" if desired == "paused" else "stopping")
        self._save(strategy)
        if strategy["mode"] == "paper":
            for slot in strategy["slots"]:
                for leg in ("entry", "exit"):
                    order = slot.get(leg)
                    if order and order["status"] not in TERMINAL:
                        order.update(status="CANCELED", state="terminal")
                self._settle_slot(strategy, slot)
            strategy["status"] = desired
            self._save(strategy)
        else:
            client = self.client_factory(account)
            self._reconcile(strategy, client, market)
            self._cancel_owned(strategy, client, market, entries_only=False)
            self._finish_halt(strategy)
            self._save(strategy)
        self._event(strategy, "warning", "已请求撤销策略挂单；已有持仓保留，可恢复策略继续减仓")
        return strategy

    def _unresolved(self, strategy):
        return any(order and order["status"] not in TERMINAL and order["state"] in (
            "pending_submission", "recovery_error", "cancel_pending") for slot in strategy["slots"]
                   for order in (slot.get("entry"), slot.get("exit")))

    def _active_orders(self, strategy):
        return any(order and order["status"] not in TERMINAL for slot in strategy["slots"]
                   for order in (slot.get("entry"), slot.get("exit")))

    def _finish_halt(self, strategy):
        desired = strategy.get("desiredStatus")
        if desired in ("paused", "stopped"):
            strategy["status"] = desired if not self._active_orders(strategy) else (
                "pausing" if desired == "paused" else "stopping")

    def _fault(self, strategy, message):
        changed = strategy.get("error") != message
        strategy.update(suspended=True, error=message, status="recovering")
        self._save(strategy)
        if changed:
            self._event(strategy, "error", message)

    def _best(self, market, name):
        raw = market.get(name)
        if isinstance(raw, dict):
            raw = raw.get("price")
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        if raw is None:
            raw = market.get("bid" if name == "bestBid" else "ask")
        try:
            number = decimal(raw)
            return number if number > 0 else None
        except ValueError:
            return None

    def _can_rest(self, side, price, market):
        bid, ask = self._best(market, "bestBid"), self._best(market, "bestAsk")
        if bid is None or ask is None or bid >= ask:
            return False
        return price < ask if side == "BUY" else price > bid

    def _current_price(self, market):
        # markPrice=0 explicitly means there is no authoritative mark (API docs).
        value = market.get("markPrice", market.get("lastTradePrice"))
        price = decimal(value, "实时标记价格")
        if price <= 0:
            raise ValueError("市场尚无有效实时标记价格")
        return price

    def tick(self, strategy, account, market):
        if strategy["mode"] == "paper":
            return self._paper_tick(strategy, market)
        client = self.client_factory(account)
        self._reconcile(strategy, client, market)
        if strategy.get("desiredStatus") in ("paused", "stopped"):
            self._cancel_owned(strategy, client, market, entries_only=False)
            self._finish_halt(strategy)
            self._save(strategy)
            return strategy
        if budget_type(strategy) == 'margin':
            validation = self.preview(strategy, market)
            if validation['errors']:
                self._fault(strategy, '合约保证金条件已变化：' + '；'.join(validation['errors']))
            elif decimal(strategy['quantity']) > decimal(validation['quantity']):
                self._fault(strategy, '当前保证金要求已提高，原网格数量超出投入预算，已暂停新开仓')
            setup = strategy.get('marginSetup', {})
            if strategy.get('status') == 'configuring' or now_ms() - setup.get('verifiedAt', 0) > 15000:
                if not self._ensure_margin(strategy, account, market, allow_write=False):
                    self._cancel_owned(strategy, client, market, entries_only=True)
                    self._save(strategy)
                    if not strategy.get('hasStarted'):
                        return strategy
                if not strategy.get('hasStarted') and strategy.get('desiredStatus') == 'running':
                    if (not strategy.get('suspended') and self._refresh_margin_account(strategy, account, market)):
                        strategy.update(status='running', hasStarted=True, suspended=False, error=None)
        snapshot = self._snapshot(account)
        if not snapshot.get("connected") or now_ms() - snapshot.get("updatedAt", 0) > 45000:
            self._fault(strategy, "账户状态未同步，已暂停新开仓")
        else:
            if budget_type(strategy) == 'margin':
                try:
                    if decimal(snapshot.get('availableBalance', snapshot.get('freeCollateral')), '可用保证金') < self.remaining_collateral(strategy, account, market):
                        self._fault(strategy, '当前可用保证金不足以覆盖剩余网格预算，已暂停新开仓')
                except ValueError as exc:
                    self._fault(strategy, str(exc))
            own_ids = {o['clientId'] for slot in strategy['slots']
                       for o in (slot.get('entry'), slot.get('exit')) if o}
            own_server_ids = {str(o['orderId']) for slot in strategy['slots']
                              for o in (slot.get('entry'), slot.get('exit')) if o and o.get('orderId')}
            if any(self._matches_market(o, strategy) and o.get('clientId') not in own_ids
                   and not str(o.get('clientId') or '').startswith(f"ag-{strategy['id'][:12]}-")
                   and str(o.get('orderId')) not in own_server_ids
                   for o in snapshot.get('orders', snapshot.get('openOrders', []))):
                self._fault(strategy, '检测到其他程序或手工挂单，已暂停新开仓；其他订单保持原状')
            # Account snapshots and order records can arrive at different times.
            # Compare only a later snapshot after a grace interval, never guess a fill.
            last_change = strategy.get('lastInventoryChangeAt', strategy['createdAt'])
            if snapshot.get('updatedAt', 0) >= last_change + 15000 and not self._unresolved(strategy):
                actual = sum((self._position_size(p) for p in snapshot.get('positions', [])
                              if self._matches_market(p, strategy)), ZERO)
                expected = decimal(strategy.get('residualQuantity', strategy['inventory']))
                if strategy['direction'] == 'short':
                    expected = -expected
                if actual != expected:
                    self._fault(strategy, '交易所持仓与网格库存不一致，已暂停新开仓，请核对手工交易或强平记录')
        try:
            price = self._current_price(market)
            strategy["currentPrice"] = string(price)
            strategy["outOfRange"] = not decimal(strategy["lower"]) <= price <= decimal(strategy["upper"])
        except ValueError as exc:
            self._fault(strategy, str(exc))
            strategy["outOfRange"] = True
        if str(market.get("status", "ONLINE")).upper() != "ONLINE":
            self._fault(strategy, "市场暂不可交易，已暂停新开仓")
        if strategy.get("suspended"):
            self._cancel_owned(strategy, client, market, entries_only=True)
        for slot in strategy["slots"]:
            self._settle_slot(strategy, slot)
            if slot.get("retryAfter", 0) > now_ms():
                continue
            inventory = decimal(slot["inventory"])
            if inventory > 0 and not self._nonterminal(slot.get("entry")) and not self._nonterminal(slot.get("exit")):
                side = "SELL" if strategy["direction"] == "long" else "BUY"
                if self._can_rest(side, decimal(slot["exitPrice"]), market):
                    self._submit(strategy, slot, "exit", side, inventory, client, market)
            elif (inventory == 0 and not self._nonterminal(slot.get("entry")) and
                  not self._nonterminal(slot.get("exit")) and not strategy.get("suspended") and
                  strategy["status"] == "running" and not strategy["outOfRange"]):
                side = "BUY" if strategy["direction"] == "long" else "SELL"
                if self._can_rest(side, decimal(slot["entryPrice"]), market):
                    self._submit(strategy, slot, "entry", side, decimal(slot["quantity"]), client, market)
        if strategy.get("suspended"):
            self._cancel_owned(strategy, client, market, entries_only=True)
            strategy["status"] = "recovering" if self._active_orders(strategy) else "error"
        self._save(strategy)
        return strategy

    @staticmethod
    def _nonterminal(order):
        return bool(order and order["status"] not in TERMINAL)

    def _new_order(self, strategy, slot, leg, side, size):
        # Arcus caps IDs at 36 characters. Length must not grow with cycle count.
        return {"clientId": f"ag-{strategy['id'][:12]}-{uuid.uuid4().hex[:16]}",
                "orderId": None, "side": side, "price": slot[f"{leg}Price"], "size": string(size),
                "filledSize": "0", "accountedFill": "0", "avgFillPrice": None,
                "state": "pending_submission", "status": "UNKNOWN", "reduceOnly": leg == "exit",
                "timeInForce": "ALO", "submittedAt": now_ms(), "cancelRequestedAt": None,
                "error": None, "uncertain": False}

    def _submit(self, strategy, slot, leg, side, size, client, market):
        order = self._new_order(strategy, slot, leg, side, size)
        slot[leg] = order
        slot["state"] = "pending_submission"
        self._save(strategy)  # Must commit before the write request, including after a restart.
        try:
            response = client.place_order(market, side, order["price"], order["size"],
                                          order["clientId"], leg == "exit")
            if isinstance(response, dict):
                order["orderId"] = response.get("orderId")
                order["error"] = response.get("rejectionReason", response.get("error"))
                gateway_status = str(response.get("status", "UNKNOWN")).upper()
                order['gatewayStatus'] = gateway_status
                reason = str(response.get('rejectionReason') or response.get('error') or '')
                confirmed_zero = response.get('filledSize') is not None and decimal(response['filledSize']) == 0
                # These reasons explicitly reject execution before any fill (official enum).
                pretrade_zero = reason in ('POST_ONLY_WOULD_CROSS', 'REDUCE_ONLY_WOULD_INCREASE',
                                          'UNDERCOLLATERALIZED', 'TOO_MANY_CLIENT_IDS', 'OPEN_ORDER_CAP_EXCEEDED')
                if gateway_status == 'REJECTED' and (confirmed_zero or pretrade_zero):
                    order.update(status='REJECTED', state='terminal', filledSize='0')
                    slot['retryAfter'] = now_ms() + 10000
                    self._event(strategy, 'warning', f"挂单被拒绝：{reason or 'REJECTED'}")
                    if reason != 'POST_ONLY_WOULD_CROSS':
                        self._fault(strategy, '交易所拒绝挂单，已暂停新开仓，请检查账户与参数')
                elif not order['orderId'] or gateway_status == 'UNKNOWN':
                    order.update(state='recovery_error', uncertain=True)
                    self._fault(strategy, '交易所未返回可核对的订单结果，已冻结新开仓等待对账')
            else:
                order.update(state='recovery_error', uncertain=True)
                self._fault(strategy, '交易所下单响应格式未确认，已冻结新开仓等待对账')
            # Even a synchronous response is reconciled independently via order_status.
            self._save(strategy)
        except Exception as exc:
            message = safe_error(exc)
            order["error"] = message
            order["uncertain"] = getattr(exc, "uncertain", True)
            if order["uncertain"]:
                order["state"] = "recovery_error"
                slot["state"] = "recovery_error"
                self._fault(strategy, "下单结果未确认，已冻结新开仓并保留订单标识等待对账")
            else:
                order.update(status="REJECTED", state="terminal")
                slot["retryAfter"] = now_ms() + max(5000, getattr(exc, "retry_after_ms", 0) or 0)
                self._fault(strategy, f"挂单被拒绝：{message}")
            self._save(strategy)

    def _cancel(self, strategy, slot, leg, client, market):
        order = slot.get(leg)
        if not self._nonterminal(order):
            return
        # Never cancel or modify an external order; IDs are minted and persisted here.
        if not order["clientId"].startswith(f"ag-{strategy['id'][:12]}-"):
            self._fault(strategy, "订单标识不属于本策略，已拒绝撤销")
            return
        if order.get("cancelRequestedAt") and now_ms() - order["cancelRequestedAt"] < 10000:
            return
        order.update(state="cancel_pending", cancelRequestedAt=now_ms())
        slot["state"] = "cancel_pending"
        self._save(strategy)
        try:
            client.cancel_order(market, order["clientId"], order.get("orderId"))
        except Exception as exc:
            # A cancel timeout also needs terminal reconciliation. Repeating a cancel
            # of an owned ID is safe; repeating an uncertain placement is not.
            order["error"] = safe_error(exc)
        self._save(strategy)

    def _cancel_owned(self, strategy, client, market, entries_only):
        for slot in strategy["slots"]:
            for leg in ("entry",) if entries_only else ("entry", "exit"):
                self._cancel(strategy, slot, leg, client, market)

    def _reconcile(self, strategy, client, market):
        for slot in strategy["slots"]:
            for leg in ("entry", "exit"):
                order = slot.get(leg)
                if not self._nonterminal(order):
                    continue
                try:
                    response = client.order_status(market, order["clientId"], order.get("orderId"))
                    status = str(response.get("status", "UNKNOWN")).upper()
                    if status in FORCED:
                        order["state"] = "recovery_error"
                        self._fault(strategy, "交易所报告强平或自动减仓；请核对账户持仓后人工处理")
                        continue
                    if status not in TERMINAL | OPEN:
                        # Absence from openOrders is not evidence of fill or rejection.
                        if now_ms() - order['submittedAt'] > 30000:
                            order['state'] = 'recovery_error'
                            self._fault(strategy, '订单超过 30 秒未确认，已冻结新开仓等待对账')
                        continue
                    filled_raw = response.get("filledSize")
                    if filled_raw is None:
                        self._fault(strategy, "订单对账缺少已成交数量，已暂停新开仓")
                        continue
                    filled = decimal(filled_raw, "已成交数量")
                    accounted = decimal(order["accountedFill"])
                    if filled < accounted or filled < 0 or filled > decimal(order["size"]):
                        self._fault(strategy, "订单对账数量异常，已暂停新开仓")
                        continue
                    if status == "FILLED" and filled != decimal(order["size"]):
                        self._fault(strategy, "成交终态与数量不一致，已暂停新开仓")
                        continue
                    average_raw = response.get("avgFillPrice")
                    if filled > 0 and average_raw is None:
                        if order.get('observedFilledSize') != string(filled):
                            strategy['lastInventoryChangeAt'] = now_ms()
                        order.update(observedFilledSize=string(filled), state="recovery_error")
                        self._fault(strategy, "已确认成交数量但缺少交易所成交均价，已保留数量并暂停新开仓等待对账")
                        continue
                    average = decimal(average_raw) if average_raw is not None else decimal(order["price"])
                    if average <= 0:
                        raise ValueError("订单成交均价无效")
                    delta = filled - accounted
                    if delta:
                        strategy['lastInventoryChangeAt'] = now_ms()
                    if leg == "entry":
                        if delta:
                            slot["inventory"] = string(decimal(slot["inventory"]) + delta)
                        # avgFillPrice is cumulative; overwrite cumulative entry cost.
                        slot["entryCost"] = string(filled * average)
                    elif delta:
                        inventory = decimal(slot["inventory"])
                        if delta > inventory:
                            self._fault(strategy, "减仓成交数量超过策略库存，已暂停新开仓")
                            continue
                        entry_average = decimal(slot["entryCost"]) / inventory if inventory else ZERO
                        old_exit_notional = decimal(order.get("accountedNotional", "0"))
                        exit_notional = filled * average
                        delta_notional = exit_notional - old_exit_notional
                        profit = delta_notional - delta * entry_average
                        if strategy["direction"] == "short":
                            profit = -profit
                        stats = strategy["stats"]
                        stats["grossProfit"] = string(decimal(stats["grossProfit"]) + profit)
                        stats["realizedPnl"] = stats["grossProfit"]
                        slot["inventory"] = string(inventory - delta)
                        slot["entryCost"] = string(decimal(slot["entryCost"]) - delta * entry_average)
                        order["accountedNotional"] = string(exit_notional)
                    order.update(status=status, filledSize=string(filled), accountedFill=string(filled),
                                 observedFilledSize=string(filled),
                                 avgFillPrice=string(average) if filled else None,
                                 orderId=response.get("orderId") or order.get("orderId"),
                                 error=response.get("error", response.get("rejectionReason")))
                    if status in TERMINAL:
                        order["state"] = "terminal"
                        if filled > 0 and not order.get("countedFill"):
                            strategy["stats"]["filledOrders"] += 1
                            order["countedFill"] = True
                        if status == "REJECTED":
                            slot["retryAfter"] = now_ms() + 10000
                            self._event(strategy, "warning", f"网格 {slot['index'] + 1} 挂单被拒绝，保留 ALO 挂单规则")
                    elif order["state"] != "cancel_pending":
                        order["state"] = f"{leg}_open"
                    self._save(strategy)
                    if leg == "entry" and filled > 0 and status not in TERMINAL:
                        self._cancel(strategy, slot, leg, client, market)
                except Exception as exc:
                    order["error"] = safe_error(exc)
                    self._fault(strategy, "订单对账失败，已暂停新开仓并保留原订单等待恢复")
            self._settle_slot(strategy, slot)

    def _settle_slot(self, strategy, slot):
        entry, exit_order = slot.get("entry"), slot.get("exit")
        if self._nonterminal(entry):
            slot["state"] = entry["state"]
            return
        if self._nonterminal(exit_order):
            slot["state"] = exit_order["state"]
            return
        if decimal(slot["inventory"]) > 0:
            slot["state"] = "inventory"
            return
        if entry or exit_order:
            if exit_order and decimal(exit_order["filledSize"]) > 0 and not slot.get("cycleCounted"):
                strategy["stats"]["cycles"] += 1
            slot.update(entry=None, exit=None, entryCost="0", cycle=slot["cycle"] + 1, state="idle")
        else:
            slot["state"] = "idle"

    def _paper_tick(self, strategy, market):
        if strategy["status"] != "running":
            self._finish_halt(strategy)
            self._save(strategy)
            return strategy
        price = self._current_price(market)
        previous_raw = strategy.get("paperLastPrice")
        previous = decimal(previous_raw) if previous_raw is not None else price
        strategy.update(currentPrice=string(price), outOfRange=not (
            decimal(strategy["lower"]) <= price <= decimal(strategy["upper"])))
        for slot in strategy["slots"]:
            entry, exit_order = slot.get("entry"), slot.get("exit")
            # Only a later observed live-price crossing fills a simulated order.
            # A single sample cannot fill an entry and its newly created exit.
            if self._nonterminal(entry):
                level = decimal(entry["price"])
                crossed = (previous >= level >= price and previous > price) if entry["side"] == "BUY" else (
                    previous <= level <= price and previous < price)
                if crossed:
                    quantity = decimal(entry["size"])
                    entry.update(status="FILLED", state="terminal", filledSize=entry["size"], accountedFill=entry["size"])
                    slot.update(inventory=string(quantity), entryCost=string(quantity * level))
                    strategy["stats"]["filledOrders"] += 1
            elif self._nonterminal(exit_order):
                level = decimal(exit_order["price"])
                crossed = (previous <= level <= price and previous < price) if exit_order["side"] == "SELL" else (
                    previous >= level >= price and previous > price)
                if crossed:
                    quantity = decimal(exit_order["size"])
                    profit = (level - decimal(slot["entryPrice"])) * quantity
                    if strategy["direction"] == "short":
                        profit = -profit
                    strategy["stats"]["grossProfit"] = string(decimal(strategy["stats"]["grossProfit"]) + profit)
                    strategy["stats"]["realizedPnl"] = strategy["stats"]["grossProfit"]
                    strategy["stats"]["filledOrders"] += 1
                    exit_order.update(status="FILLED", state="terminal", filledSize=exit_order["size"], accountedFill=exit_order["size"])
                    slot["inventory"] = "0"
            self._settle_slot(strategy, slot)
            if decimal(slot["inventory"]) > 0 and not self._nonterminal(slot.get("exit")):
                side = "SELL" if strategy["direction"] == "long" else "BUY"
                if self._can_rest(side, decimal(slot["exitPrice"]), market):
                    slot["exit"] = self._new_order(strategy, slot, "exit", side, decimal(slot["inventory"]))
                    slot["exit"].update(status="OPEN", state="exit_open")
            elif (not self._nonterminal(slot.get("entry")) and not self._nonterminal(slot.get("exit"))
                  and decimal(slot["inventory"]) == 0 and not strategy["outOfRange"]):
                side = "BUY" if strategy["direction"] == "long" else "SELL"
                if self._can_rest(side, decimal(slot["entryPrice"]), market):
                    slot["entry"] = self._new_order(strategy, slot, "entry", side, decimal(slot["quantity"]))
                    slot["entry"].update(status="OPEN", state="entry_open")
            self._settle_slot(strategy, slot)
        strategy["paperLastPrice"] = string(price)
        strategy["stats"]["fees"] = None
        self._save(strategy)
        return strategy
