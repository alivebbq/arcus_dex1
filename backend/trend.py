"""Arcus adaptation of the verified running quant ETH SuperTrend strategy.

Indicator arithmetic is copied with attribution from nakedquant's pure streaming
implementation (SHA256 D0405F9B663912F058B8CE87A5759B42044BD97CBF4A0DDEFAF7B4815175EA41).
Source execution evidence is retained locally outside the published repository. Venue history, allocated
capital, protective order APIs, and fee/funding accounting are venue adaptations.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite
import time
import uuid

from .grid import GridEngine, OPEN, TERMINAL, ZERO, decimal, now_ms, quantize_price, safe_error, string
from .margin import active_margin_fraction

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
RULES = {"source": "quant 当前执行的 ETH SuperTrend", "market": "ETH-USD", "timeframe": "1h",
         "atrPeriod": 14, "adxPeriod": 14, "adxMin": 20, "bandAtr": 4,
         "stopAtr": "3.5", "tradeRisk": "0.03", "dailyLossLimit": "0.15", "breakevenR": "2",
         "entryOffset": "0.5", "exitOffset": "1", "stopLimitOffset": "0.2",
         "entryTimeoutMs": 60000, "exitTimeoutMs": 60000, "stopTimeoutMs": 10000,
         "maxSignalAgeMs": 90000, "reverseImmediately": False,
         "capitalBasis": "min_allocated_margin_and_available_collateral",
         "historyBasis": "Arcus closed candles; indicator rules match quant, venue history differs"}


@dataclass(frozen=True)
class Bar:
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    is_closed: bool = True

    def __post_init__(self):
        if any(not p.is_finite() or p <= 0 for p in (self.open, self.high, self.low, self.close)):
            raise ValueError("K 线价格必须是正数")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("K 线 OHLC 不一致")


@dataclass(frozen=True)
class SuperTrendReading:
    open_time_ms: int
    atr: float
    adx: float | None
    trend: int
    flip: int
    entry_signal: int


class SuperTrendIndicators:
    """Exact HA-close band/ATR/ADX arithmetic of the verified quant source."""
    def __init__(self, period=14, multiplier=4.0, adx_min=20.0):
        if (type(period) is not int or period < 2 or not isfinite(multiplier) or multiplier <= 0
                or not isfinite(adx_min) or adx_min < 0):
            raise ValueError("invalid indicator parameters")
        self.period, self.multiplier, self.adx_min = period, multiplier, adx_min
        self.last = None
        self._ha = None
        self._plus = self._minus = 0.0
        self._upper = self._lower = 0.0
        self._adx_weight = 1.0

    def update(self, bar):
        if (not bar.is_closed or bar.open_time_ms % HOUR_MS
                or bar.close_time_ms + 1 != bar.open_time_ms + HOUR_MS):
            raise ValueError("completed whole-hour bar required")
        if self.last is not None and bar.open_time_ms != self.last.open_time_ms + HOUR_MS:
            raise ValueError("duplicate, out-of-order, or missing bar")
        o, h, l, c = map(float, (bar.open, bar.high, bar.low, bar.close))
        hc = (o + h + l + c) / 4
        ho = (o + c) / 2 if self._ha is None else (self._ha[0] + self._ha[3]) / 2
        hh, hl = max(h, ho, hc), min(l, ho, hc)
        alpha, beta = 1 / self.period, 1 - 1 / self.period
        if self.last is None:
            atr, trend, flip, adx = hh - hl, -1, 0, None
            self._upper, self._lower = hc + self.multiplier * atr, hc - self.multiplier * atr
        else:
            _, ph, pl, pc = self._ha
            tr = max(hh - hl, abs(hh - pc), abs(hl - pc))
            atr = beta * self.last.atr + alpha * tr
            up, down = hh - ph, pl - hl
            self._plus = beta * self._plus + alpha * (up if up > down and up > 0 else 0)
            self._minus = beta * self._minus + alpha * (down if down > up and down > 0 else 0)
            pdi, mdi = (100 * self._plus / atr, 100 * self._minus / atr) if atr else (0, 0)
            dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else None
            adx = self.last.adx
            if adx is not None:
                self._adx_weight *= beta
            if dx is not None:
                adx = dx if adx is None else (self._adx_weight * adx + alpha * dx) / (self._adx_weight + alpha)
                self._adx_weight = 1.0
            trend = self.last.trend
            if (trend == 1 and hc < self._lower) or (trend == -1 and hc > self._upper):
                trend = -trend
            flip = trend if trend != self.last.trend else 0
            upper, lower = hc + self.multiplier * atr, hc - self.multiplier * atr
            if trend == 1:
                self._upper, self._lower = upper, max(self._lower, lower)
            else:
                self._upper, self._lower = min(self._upper, upper), lower
        signal = flip if adx is not None and adx > self.adx_min and atr > 0 else 0
        self._ha = ho, hh, hl, hc
        self.last = SuperTrendReading(bar.open_time_ms, atr, adx, trend, flip, signal)
        return self.last

    def export(self):
        return {"last": asdict(self.last) if self.last else None, "ha": self._ha,
                "plus": self._plus, "minus": self._minus, "upper": self._upper,
                "lower": self._lower, "adxWeight": self._adx_weight}

    @classmethod
    def restore(cls, state):
        engine = cls()
        if state and state.get("last"):
            engine.last = SuperTrendReading(**state["last"])
            engine._ha = tuple(state["ha"])
            engine._plus, engine._minus = state["plus"], state["minus"]
            engine._upper, engine._lower = state["upper"], state["lower"]
            engine._adx_weight = state["adxWeight"]
        return engine


def closed_bars(candles, timestamp=None):
    timestamp = now_ms() if timestamp is None else timestamp
    result = []
    for row in candles:
        raw = row.get("openTime", row.get("startTime", row.get("time", row.get("timestamp"))))
        stamp = int(decimal(raw, "K 线时间"))
        if stamp < 100_000_000_000:
            stamp *= 1000
        elif stamp > 100_000_000_000_000:
            stamp //= 1000  # Official raw startTime is microseconds.
        if stamp + HOUR_MS > timestamp:
            continue
        result.append(Bar(stamp, stamp + HOUR_MS - 1, *(decimal(row[k], "K 线价格")
                                                       for k in ("open", "high", "low", "close"))))
    result.sort(key=lambda bar: bar.open_time_ms)
    if any(b.open_time_ms != a.open_time_ms + HOUR_MS for a, b in zip(result, result[1:])):
        raise ValueError("小时 K 线重复或有缺口")
    return result


def maker_price(price, side, market, offset):
    target = price - offset if side == "BUY" else price + offset
    if target <= 0:
        raise ValueError("挂单价格必须大于 0")
    return quantize_price(target, market, ROUND_FLOOR if side == "BUY" else ROUND_CEILING)


def entry_plan(direction, price, atr, capital, available, risk_remaining, market, leverage=5):
    """Decimal sizing follows quant with Arcus effective IMF and allocated cap."""
    if direction not in (-1, 1) or atr <= 0 or capital <= 0 or available <= 0:
        raise ValueError("趋势信号或可用资金无效")
    if not isinstance(leverage, int) or isinstance(leverage, bool) or not 1 <= leverage <= 5:
        raise ValueError("趋势策略杠杆必须是 1 到 5 的整数")
    base_imf = decimal(market.get("initialMarginFraction"), "市场保证金率")
    if base_imf <= 0 or Decimal(leverage) * base_imf > 1:
        raise ValueError("趋势杠杆超过市场上限")
    maker_fee = decimal(market.get("makerFee"), "挂单费率")
    taker_fee = decimal(market.get("takerFee"), "吃单费率")
    if taker_fee < 0 or taker_fee >= 1 or abs(maker_fee) >= 1:
        raise ValueError("手续费率无效")
    side = "BUY" if direction == 1 else "SELL"
    entry = maker_price(price, side, market, Decimal(RULES["entryOffset"]))
    stop = quantize_price(entry - direction * Decimal(RULES["stopAtr"]) * atr, market,
                          ROUND_FLOOR if direction == 1 else ROUND_CEILING)
    if stop <= 0 or direction * (entry - stop) <= 0:
        raise ValueError("初始止损价格无效")
    exit_reserve = taker_fee + Decimal("0.0003")
    entry_reserve = max(ZERO, maker_fee)
    loss_per_unit = abs(entry - stop) + entry * entry_reserve + stop * exit_reserve
    risk_budget = min(capital * Decimal(RULES["tradeRisk"]), risk_remaining)
    if risk_budget <= 0:
        raise ValueError("本日日风险额度已用尽")
    imf = max(Decimal(1) / leverage, active_margin_fraction(market))
    valuation = max(entry, price)
    per_unit_collateral = valuation * imf + entry * entry_reserve + stop * exit_reserve
    cap = min(risk_budget / loss_per_unit, capital * leverage / entry,
              available * Decimal("0.98") / per_unit_collateral)
    if market.get("maxOrderSize") is not None:
        cap = min(cap, decimal(market["maxOrderSize"]))
    if market.get("positionSizeCapNotional") is not None:
        cap = min(cap, decimal(market["positionSizeCapNotional"]) / entry)
    step = decimal(market["stepSize"])
    quantity = (cap / step).to_integral_value(rounding=ROUND_FLOOR) * step
    minimum_qty = max(decimal(market.get("minOrderSize", step)), decimal(market["minOrderNotional"]) / entry)
    minimum_qty = (minimum_qty / step).to_integral_value(rounding=ROUND_CEILING) * step
    minimum_capital = max(minimum_qty * loss_per_unit / Decimal(RULES["tradeRisk"]),
                          minimum_qty * entry / leverage, minimum_qty * per_unit_collateral / Decimal("0.98"))
    if quantity < minimum_qty:
        error = ValueError("资金不足以同时满足 3% 计划风险与最小订单金额")
        error.minRequiredRiskCapital = string(minimum_capital)
        raise error
    fee_reserve = quantity * (entry * entry_reserve + stop * exit_reserve)
    return {"direction": direction, "side": side, "quantity": string(quantity), "limit": string(entry),
            "stop": string(stop), "initialStop": string(stop), "plannedLoss": string(quantity * loss_per_unit),
            "plannedRiskCapital": string(capital), "notional": string(quantity * entry),
            "estimatedInitialMargin": string(quantity * valuation * imf), "riskBudget": string(risk_budget),
            "marginReserve": string(fee_reserve), "collateralBuffer": string(capital * Decimal("0.02")),
            "requiredCollateral": string(quantity * valuation * imf + fee_reserve + capital * Decimal("0.02")),
            "minRequiredRiskCapital": string(minimum_capital),
            "makerFee": string(maker_fee), "stopCostReserve": string(exit_reserve)}


class TrendEngine(GridEngine):
    """One owned ETH position; opposite signals close, never reverse immediately."""
    def signals(self, market, candles=None):
        result = {"rules": dict(RULES), "signal": None, "candles": [], "warmup": False, "errors": []}
        try:
            bars = closed_bars(market.get("candles", []) if candles is None else candles)
            engine = SuperTrendIndicators()
            readings = [engine.update(bar) for bar in bars]
            result.update(warmup=len(bars) >= 200, barCount=len(bars),
                          signal={**asdict(readings[-1]), "bandPrice": engine._lower if readings[-1].trend == 1 else engine._upper}
                          if readings else None,
                          candles=[{"time": bar.open_time_ms // 1000, "open": string(bar.open),
                                    "high": string(bar.high), "low": string(bar.low), "close": string(bar.close)}
                                   for bar in bars[-200:]])
            if len(bars) < 200:
                result["errors"].append("至少需要 200 根连续已收盘小时 K 线预热")
            if market.get("candlesError"):
                result["errors"].append("小时 K 线同步失败")
        except Exception as exc:
            result["errors"].append(safe_error(exc))
        return result

    def _validate_config(self, config, market):
        if config.get("market", "ETH-USD") != "ETH-USD" or market.get("symbol", "ETH-USD") != "ETH-USD":
            raise ValueError("当前趋势规则仅支持 ETH-USD 永续合约")
        budget = decimal(config.get("budget"), "投入保证金")
        if not 0 < budget <= 1000000:
            raise ValueError("投入保证金必须大于 0 且不超过 1,000,000")
        leverage = config.get("leverage", 5)
        if not isinstance(leverage, int) or isinstance(leverage, bool) or not 1 <= leverage <= 5:
            raise ValueError("趋势策略杠杆必须是 1 到 5 的整数")
        if config.get("marginMode", "isolated") not in ("cross", "isolated"):
            raise ValueError("保证金模式必须是 cross 或 isolated")
        if config.get("mode", "paper") not in ("live", "paper"):
            raise ValueError("执行模式必须是 live 或 paper")
        base = decimal(market.get("initialMarginFraction"), "市场保证金率")
        if base <= 0 or Decimal(leverage) * base > 1:
            raise ValueError("趋势杠杆超过市场上限")
        return budget, leverage

    def preview(self, config, market):
        public = self.signals(market)
        result = {**public, "errors": [], "warnings": list(public["errors"]), "eligible": False,
                  "plan": None, "quantity": "0", "plannedLoss": None,
                  "capitalBasis": RULES["capitalBasis"], "profitBasis": "gross_before_fees_and_funding"}
        try:
            budget, leverage = self._validate_config(config, market)
            result.update(allocatedMargin=string(budget), leverage=leverage,
                          marginMode=config.get("marginMode", "isolated"), notionalBudget=string(budget * leverage))
        except ValueError as exc:
            result["errors"].append(str(exc))
            return result
        reading = public["signal"]
        if not public["warmup"] or reading is None:
            return result
        direction = reading["entry_signal"] or reading["trend"]
        try:
            price = self._current_price(market)
            available = decimal(market.get("availableForStrategy", budget))
            capital = min(budget, available)
            plan = entry_plan(direction, price, decimal(reading["atr"]), capital, available,
                              budget * Decimal(RULES["dailyLossLimit"]), market, leverage)
            result.update(plan=plan, quantity=plan["quantity"], plannedLoss=plan["plannedLoss"],
                          estimatedInitialMargin=plan["estimatedInitialMargin"], orderNotional=plan["notional"],
                          requiredCollateral=plan["requiredCollateral"], marginReserve=plan["marginReserve"],
                          collateralBuffer=plan["collateralBuffer"], minRequiredRiskCapital=plan["minRequiredRiskCapital"],
                          riskCapital=plan["plannedRiskCapital"], plannedRisk=plan["plannedLoss"],
                          eligible=bool(reading["entry_signal"] and not public["errors"]))
            if not reading["entry_signal"]:
                result["warnings"].append("当前小时线没有满足 ADX>20 的新翻转信号，等待下一次有效信号")
        except ValueError as exc:
            result["warnings"].append(str(exc))
            if hasattr(exc, "minRequiredRiskCapital"):
                result["minRequiredRiskCapital"] = exc.minRequiredRiskCapital
        return result

    def create(self, config, market):
        budget, leverage = self._validate_config(config, market)
        strategy = {"id": uuid.uuid4().hex, "type": "trend", "name": str(config.get("name", "ETH SuperTrend 合约趋势"))[:80],
                    "accountId": config["accountId"], "market": "ETH-USD", "marketId": market["marketId"],
                    "mode": config.get("mode", "paper"), "budgetType": "margin", "budget": string(budget),
                    "allocatedMargin": string(budget), "leverage": leverage,
                    "marginMode": config.get("marginMode", "isolated"), "notionalBudget": string(budget * leverage),
                    "direction": "long", "quantity": "0", "status": "draft", "desiredStatus": "paused",
                    "createdAt": now_ms(), "hasStarted": False, "error": None, "suspended": False,
                    "rules": dict(RULES), "signal": None, "indicatorState": None, "lastSignalClosedAt": 0,
                    "trade": None, "protection": None, "emergency": None, "orderRegistry": {},
                    "inventory": "0", "residualQuantity": "0", "position": None, "orders": [],
                    "accounting": {"known": True, "fees": "0", "funding": "0", "netRealized": "0", "fills": {}},
                    "stats": {"cycles": 0, "filledOrders": 0, "grossProfit": "0", "realizedPnl": "0",
                              "fees": None, "netRealizedPnl": None, "profitBasis": "gross_before_fees_and_funding"},
                    "slots": [{"index": 0, "entryPrice": "0", "exitPrice": "0", "quantity": "0",
                               "cycle": 0, "state": "idle", "inventory": "0", "entryCost": "0",
                               "entry": None, "exit": None, "retryAfter": 0, "error": None}]}
        self._save(strategy)
        return strategy

    def _save(self, strategy):
        registry = strategy.setdefault("orderRegistry", {})
        records = [strategy.get("protection"), strategy.get("emergency")]
        for slot in strategy["slots"]:
            records.extend((slot.get("entry"), slot.get("exit")))
        for order in records:
            if order:
                registry[order["clientId"]] = {"orderId": order.get("orderId"), "submittedAt": order["submittedAt"],
                                               "leg": order.get("leg"), "size": order.get("size"),
                                               "filledSize": string(max(decimal(order.get("accountedFill", "0")),
                                                                        decimal(order.get("observedFilledSize", "0")),
                                                                        decimal(order.get("filledSize", "0"))))}
        super()._save(strategy)
        slot = strategy["slots"][0]
        for leg in ("protection", "emergency"):
            order = strategy.get(leg)
            if order and order.get("observedFilledSize") is not None:
                unaccounted = max(ZERO, decimal(order["observedFilledSize"]) - decimal(order["accountedFill"]))
                strategy["residualQuantity"] = string(decimal(strategy["residualQuantity"]) - unaccounted)
        inventory = decimal(strategy["residualQuantity"])
        strategy["position"] = ({"market": strategy["market"], "side": strategy["direction"],
                                 "size": string(inventory),
                                 "entryPrice": string(decimal(slot["entryCost"]) / decimal(slot["inventory"]))
                                 if decimal(slot["inventory"]) else None,
                                 "stopPrice": (strategy.get("trade") or {}).get("activeStop"),
                                 "protectionStatus": (strategy.get("protection") or {}).get("status")}
                                if inventory else None)
        for leg in ("protection", "emergency"):
            order = strategy.get(leg)
            if order and order["status"] not in TERMINAL:
                strategy["orders"].append({"slot": 0, "leg": leg, **order})
        self.store.save_strategy(strategy)

    def remaining_collateral(self, strategy, account, market):
        if strategy["status"] == "draft":
            return ZERO
        if strategy["status"] == "stopped" and decimal(strategy.get("residualQuantity", "0")) == 0:
            return ZERO
        # The allocation is reserved once; exchange freeCollateral already excludes
        # actual position margin, so only reserve the unused part for other panels.
        return super().remaining_collateral(strategy, account, market)

    def _update_indicator(self, strategy, market):
        if market.get("candlesError"):
            raise ValueError("小时 K 线同步失败，暂停新开仓")
        if market.get("candlesUpdatedAt") and now_ms() - market["candlesUpdatedAt"] > 90000:
            raise ValueError("小时 K 线缓存已过期")
        bars = closed_bars(market.get("candles", []))
        engine = SuperTrendIndicators.restore(strategy.get("indicatorState"))
        bootstrap = engine.last is None
        if bootstrap and len(bars) < 200:
            raise ValueError("趋势指标至少需要 200 根连续已收盘小时 K 线")
        fresh = [bar for bar in bars if engine.last is None or bar.open_time_ms > engine.last.open_time_ms]
        readings = []
        for bar in fresh:
            readings.append((engine.update(bar), bar))
        strategy["indicatorState"] = engine.export()
        strategy["signal"] = ({**asdict(engine.last), "bandPrice": engine._lower if engine.last.trend == 1 else engine._upper}
                              if engine.last else None)
        if bootstrap and engine.last:
            # No order may be driven by a historical signal seen during activation.
            strategy["lastSignalClosedAt"] = engine.last.open_time_ms + HOUR_MS
            strategy["indicatorOrigin"] = bars[0].open_time_ms
            readings = []
        self._save(strategy)
        return readings

    def start(self, strategy, account, market):
        self._validate_config(strategy, market)
        snapshot = self._snapshot(account)
        if strategy["mode"] == "live":
            if not snapshot.get("connected") or now_ms() - snapshot.get("updatedAt", 0) > 45000:
                raise ValueError("账户状态未连接或超过 45 秒未同步")
            self._check_ownership(strategy, snapshot)
            if not strategy.get("hasStarted") and decimal(snapshot.get("availableBalance")) < decimal(strategy["budget"]):
                raise ValueError("可用保证金不足以覆盖策略分配额度")
            if not self._ensure_margin(strategy, account, market, allow_write=True):
                self._save(strategy)
                return strategy
            fresh = self.client_factory(account).account_snapshot()
            fresh["updatedAt"] = fresh.get("updatedAt", fresh.get("updatedAtMs", now_ms()))
            if not fresh.get("connected"):
                raise ValueError("合约配置确认后账户状态未连接")
            self._check_ownership(strategy, fresh)
            if not strategy.get("hasStarted") and decimal(fresh.get("availableBalance")) < decimal(strategy["budget"]):
                raise ValueError("合约配置确认后可用保证金不足")
            account["snapshot"] = fresh
        self._update_indicator(strategy, market)
        strategy.update(status="running", desiredStatus="running", suspended=False, error=None,
                        hasStarted=True, activatedAt=strategy.get("activatedAt", now_ms()))
        self._save(strategy)
        return self.tick(strategy, account, market)

    def _check_ownership(self, strategy, snapshot):
        registry = strategy["orderRegistry"]
        server_ids = {str(row["orderId"]) for row in registry.values() if row.get("orderId")}
        if any(self._matches_market(order, strategy) and order.get("clientId") not in registry
               and str(order.get("orderId")) not in server_ids
               for order in snapshot.get("orders", snapshot.get("openOrders", []))):
            raise ValueError("ETH-USD 有非本策略订单，不能同时手工交易该市场")
        actual = sum((self._position_size(row) for row in snapshot.get("positions", [])
                      if self._matches_market(row, strategy)), ZERO)
        expected = decimal(strategy.get("residualQuantity", "0")) * (1 if strategy["direction"] == "long" else -1)
        if actual != expected:
            raise ValueError("ETH-USD 持仓与策略库存不一致，请核对其他程序或手工交易")

    def pause(self, strategy, account, market):
        return self._halt(strategy, account, market, "paused")

    def stop(self, strategy, account, market):
        return self._halt(strategy, account, market, "stopped")

    def _halt(self, strategy, account, market, desired):
        strategy.update(desiredStatus=desired, status="pausing" if desired == "paused" else "stopping")
        self._save(strategy)
        # Stop opening exposure, retain the native protective stop for owned inventory.
        if strategy["mode"] == "paper":
            for leg in ("entry",):
                order = strategy["slots"][0].get(leg)
                if self._nonterminal(order):
                    order.update(status="CANCELED", state="terminal")
            strategy["status"] = desired
            self._save(strategy)
            return strategy
        return self.tick(strategy, account, market)

    def _record_order(self, strategy, leg, side, price, size, tif="ALO"):
        return {"clientId": f"ag-{strategy['id'][:12]}-{uuid.uuid4().hex[:16]}", "orderId": None,
                "leg": leg, "side": side, "price": string(price), "size": string(size),
                "filledSize": "0", "accountedFill": "0", "accountedNotional": "0",
                "state": "pending_submission", "status": "UNKNOWN", "submittedAt": now_ms(),
                "cancelRequestedAt": None, "timeInForce": tif, "reduceOnly": leg != "entry", "error": None}

    def _submit_protection(self, strategy, client, market):
        trade = strategy.get("trade")
        if not trade or strategy.get("protection"):
            return
        direction = trade["direction"]
        side = "SELL" if direction == 1 else "BUY"
        trigger = decimal(trade["initialStop"])
        # The quant limit-stop offset intentionally rests on the maker side at trigger.
        limit = quantize_price(trigger + direction * Decimal(RULES["stopLimitOffset"]), market,
                               ROUND_CEILING if side == "SELL" else ROUND_FLOOR)
        order = self._record_order(strategy, "protection", side, limit, ZERO, "GTT")
        order["stopPrice"] = string(trigger)
        order["positionTpsl"] = True
        strategy["protection"] = order
        self._save(strategy)
        try:
            ack = client.place_stop_loss(market, side, string(trigger), string(limit), order["clientId"])
            order["orderId"] = ack.get("orderId")
            if not order["orderId"] or str(ack.get("status", "UNKNOWN")).upper() in ("REJECTED", "ERROR", "UNKNOWN"):
                self._fault(strategy, "保护止损尚未确认，保留订单身份等待对账")
                trade.update(exitReason="native_protection_unconfirmed", urgent=True, exitRequestedAt=now_ms())
        except Exception as exc:
            order["error"] = safe_error(exc)
            order["state"] = "recovery_error"
            self._fault(strategy, "保护止损结果未确认，暂停开仓并等待对账")
            trade.update(exitReason="native_protection_unconfirmed", urgent=True, exitRequestedAt=now_ms())
            if getattr(exc, "uncertain", True) is False:
                order.update(status="REJECTED", state="terminal")
                trade.update(exitReason="native_protection_rejected", urgent=True,
                             exitRequestedAt=now_ms() - RULES["stopTimeoutMs"])
        self._save(strategy)

    def _cancel_record(self, strategy, order, client, market):
        if not self._nonterminal(order):
            return
        if order.get("cancelRequestedAt") and now_ms() - order["cancelRequestedAt"] < 10000:
            return
        order.update(state="cancel_pending", cancelRequestedAt=now_ms())
        self._save(strategy)
        try:
            client.cancel_order(market, order["clientId"], order.get("orderId"))
        except Exception as exc:
            order["error"] = safe_error(exc)
        self._save(strategy)

    def _reconcile_extra(self, strategy, client, market, leg):
        order = strategy.get(leg)
        if not self._nonterminal(order):
            return
        try:
            status_fn = getattr(client, "stop_status", client.order_status) if leg == "protection" else client.order_status
            row = status_fn(market, order["clientId"], order.get("orderId"))
            status = str(row.get("status", "UNKNOWN")).upper()
            if status in ("UNTRIGGERED", "TPSL_PLACED"):
                order.update(status=status, state="protected", orderId=row.get("orderId") or order.get("orderId"))
            elif status in OPEN | TERMINAL | {"TPSL_TRIGGERED", "TPSL_CANCELED"}:
                filled_raw = row.get("filledSize")
                if filled_raw is None:
                    if status not in ("TPSL_TRIGGERED",):
                        self._fault(strategy, "止损/应急订单缺少已成交数量，不能推测持仓已平")
                        return
                    filled = decimal(order["accountedFill"])
                else:
                    filled = decimal(filled_raw)
                old = decimal(order["accountedFill"])
                inventory = decimal(strategy["slots"][0]["inventory"])
                delta = filled - old
                if delta < 0 or delta > inventory:
                    raise ValueError("保护/应急成交数量与策略库存不一致")
                if delta:
                    average_raw = row.get("avgFillPrice")
                    if average_raw is None:
                        order["observedFilledSize"] = string(filled)
                        self._fault(strategy, "保护/应急成交缺少实际均价，等待对账")
                        return
                    average = decimal(average_raw)
                    slot = strategy["slots"][0]
                    entry_avg = decimal(slot["entryCost"]) / inventory
                    notional = filled * average
                    delta_notional = notional - decimal(order["accountedNotional"])
                    profit = (delta_notional - delta * entry_avg) * (1 if strategy["direction"] == "long" else -1)
                    strategy["stats"]["grossProfit"] = string(decimal(strategy["stats"]["grossProfit"]) + profit)
                    strategy["stats"]["realizedPnl"] = strategy["stats"]["grossProfit"]
                    slot.update(inventory=string(inventory - delta), entryCost=string(decimal(slot["entryCost"]) - delta * entry_avg))
                    order.update(accountedFill=string(filled), filledSize=string(filled), observedFilledSize=string(filled),
                                 accountedNotional=string(notional))
                    strategy["lastInventoryChangeAt"] = now_ms()
                order.update(status="CANCELED" if status == "TPSL_CANCELED" else status,
                             orderId=row.get("orderId") or order.get("orderId"))
                if status in TERMINAL | {"TPSL_CANCELED"}:
                    order["state"] = "terminal"
                else:
                    order["state"] = "triggered" if leg == "protection" else "exit_open"
                if leg == "protection" and (status in OPEN | {"TPSL_TRIGGERED"} or row.get("triggered") is True):
                    # Source API does not promise a trigger timestamp. First observation
                    # is persisted and is never reset by process restart.
                    order.setdefault("firstTriggeredAt", now_ms())
            elif now_ms() - order["submittedAt"] > 30000:
                self._fault(strategy, "保护/应急订单超过 30 秒未确认，保留原身份等待对账")
        except Exception as exc:
            order["error"] = safe_error(exc)
            self._fault(strategy, "保护/应急订单对账失败，暂停新开仓")
        self._save(strategy)

    def _read_accounting(self, strategy, client, market, snapshot):
        today = (now_ms() + 8 * HOUR_MS) // DAY_MS
        slot = strategy["slots"][0]
        mark = decimal(market.get("markPrice", "0"))
        # Arcus' documented position.unrealizedPnl is a margin delta, not price
        # PnL. Use known owned cost and the authoritative current mark instead.
        current_unrealized = ((decimal(slot["inventory"]) * mark - decimal(slot["entryCost"])) *
                              (1 if strategy["direction"] == "long" else -1)) if mark > 0 else ZERO
        day = strategy.setdefault("dayRisk", {"day": today, "baseline": strategy["budget"], "halted": False,
                                               "unrealizedAnchor": string(current_unrealized)})
        if day["day"] != today:
            # Daily limits can reset at the calendar boundary, not on manual resume.
            strategy["dayRisk"] = day = {"day": today, "baseline": strategy["budget"], "halted": False,
                                          "unrealizedAnchor": string(current_unrealized)}
        if strategy["mode"] == "paper":
            day.setdefault("grossAnchor", strategy["stats"]["grossProfit"])
            pnl = decimal(strategy["stats"]["grossProfit"]) - decimal(day["grossAnchor"])
            slot = strategy["slots"][0]
            if decimal(slot["inventory"]) and decimal(market.get("markPrice", "0")) > 0:
                pnl += (decimal(slot["inventory"]) * decimal(market["markPrice"]) - decimal(slot["entryCost"])) * (
                    1 if strategy["direction"] == "long" else -1)
            pnl -= decimal(day.get("unrealizedAnchor", "0"))
            known = True
        elif not strategy.get("trade") and not strategy["orderRegistry"]:
            pnl, known = ZERO, True  # No order has ever existed for this strategy.
        else:
            try:
                if mark <= 0 or decimal(strategy["residualQuantity"]) != decimal(slot["inventory"]):
                    raise ValueError("实时标记价或成交成本尚未确认，不能计算日风险")
                start = today * DAY_MS - 8 * HOUR_MS
                since = strategy.get("activatedAt", strategy["createdAt"])
                fills = client.fills(market, limit=1000, from_us=since * 1000, to_us=now_ms() * 1000)
                funding = client.funding_payments(market, since * 1000, now_ms() * 1000, limit=1000)
                if len(fills) >= 1000 or len(funding) >= 1000:
                    raise ValueError("成交或资金费账本达到分页上限，不能确认本日完整账本")
                registry = strategy["orderRegistry"]
                ids = {str(value["orderId"]) for value in registry.values() if value.get("orderId")}
                matched = [fill for fill in fills if fill.get("clientId") in registry or str(fill.get("orderId")) in ids]
                # Persist complete actual rows; closedPnl is officially net of the fill's fee.
                if any(fill.get("tradeId") is None or fill.get("closedPnl") is None or fill.get("fee") is None for fill in matched):
                    raise ValueError("成交账本缺少净盈亏或手续费证据")
                unique = {}
                for fill in matched:
                    identity = f"{fill['tradeId']}:{fill.get('orderId')}"
                    if identity in unique and unique[identity] != fill:
                        raise ValueError("同一成交身份存在冲突记录")
                    unique[identity] = fill
                fill_quantities = {}
                for fill in unique.values():
                    identity = str(fill.get("orderId"))
                    fill_quantities[identity] = fill_quantities.get(identity, ZERO) + decimal(fill.get("size"))
                if any(decimal(row.get("filledSize", "0")) > fill_quantities.get(str(row.get("orderId")), ZERO)
                       for row in registry.values() if row.get("orderId")):
                    raise ValueError("实际成交数量与手续费账本尚未对齐")
                fees = sum((decimal(fill["fee"]) for fill in unique.values()), ZERO)
                net = sum((decimal(fill["closedPnl"]) for fill in unique.values()), ZERO)
                # The strategy has exclusive control of ETH from activation onward;
                # exclude any market funding that predates activation.
                funding_by_time = {}
                for row in funding:
                    identity = (str(row.get("marketId")), int(row["time"]))
                    payment = decimal(row.get("payment"), "实际资金费")
                    if identity in funding_by_time and funding_by_time[identity] != payment:
                        raise ValueError("资金费账本存在冲突记录")
                    funding_by_time[identity] = payment
                funding_total = sum(funding_by_time.values(), ZERO)
                daily_net = sum((decimal(fill["closedPnl"]) for fill in unique.values()
                                 if int(fill.get("createdAt", 0)) >= start * 1000), ZERO)
                daily_funding = sum((value for (_, stamp), value in funding_by_time.items() if stamp >= start * 1000), ZERO)
                pnl = daily_net + daily_funding + current_unrealized - decimal(day.get("unrealizedAnchor", "0"))
                trade_since = (strategy.get("trade") or {}).get("signalClosedAt", since)
                trade_funding = sum((value for (_, stamp), value in funding_by_time.items() if stamp >= trade_since * 1000), ZERO)
                strategy["accounting"] = {"known": True, "fees": string(fees), "funding": string(funding_total),
                                          "tradeFunding": string(trade_funding),
                                          "netRealized": string(net + funding_total), "fills": unique,
                                          "updatedAt": now_ms(), "error": None}
                strategy["stats"].update(fees=string(fees), netRealizedPnl=string(net + funding_total))
                known = True
            except Exception as exc:
                strategy["accounting"].update(known=False, error=safe_error(exc))
                pnl, known = ZERO, False
        remaining = decimal(day["baseline"]) * Decimal(RULES["dailyLossLimit"]) + pnl if known else ZERO
        if known and remaining <= 0:
            day["halted"] = True
            day.setdefault("haltConfirmedAt", now_ms())
            self._fault(strategy, "本策略日亏损已达到 15%，当日恢复不会重置风险额度")
        day.update(pnl=string(pnl) if known else None, remaining=string(max(ZERO, remaining)), known=known)
        return max(ZERO, remaining) if not day["halted"] else ZERO

    def _exit(self, strategy, market, client, reason, urgent=False):
        slot = strategy["slots"][0]
        if not strategy.get("trade"):
            return
        trade = strategy["trade"]
        prior_urgent = trade.get("urgent", False)
        prior_start = trade.get("exitRequestedAt")
        if urgent and not prior_urgent and prior_start is not None:
            # A protective trigger may tighten an already resting normal exit's
            # deadline, but can never extend its previously chosen deadline.
            prior_start = min(now_ms(), prior_start + RULES["exitTimeoutMs"] - RULES["stopTimeoutMs"])
        trade.update(urgent=prior_urgent or urgent, exitRequestedAt=prior_start if prior_start is not None else now_ms())
        if urgent or not prior_urgent:
            trade["exitReason"] = reason
        self._save(strategy)
        if self._nonterminal(slot.get("entry")) or self._nonterminal(slot.get("exit")):
            return
        size = decimal(slot["inventory"])
        if size <= 0:
            return
        timeout = RULES["stopTimeoutMs"] if trade.get("urgent") else RULES["exitTimeoutMs"]
        if now_ms() - trade["exitRequestedAt"] >= timeout:
            # Once cancel/IOC fallback starts, do not recreate another resting
            # exit on each poll and indefinitely postpone that fallback.
            self._save(strategy)
            return
        side = "SELL" if strategy["direction"] == "long" else "BUY"
        price = maker_price(self._current_price(market), side, market, Decimal(RULES["exitOffset"]))
        slot["exitPrice"] = string(price)
        if self._can_rest(side, price, market):
            if strategy["mode"] == "paper":
                slot["exit"] = self._record_order(strategy, "exit", side, price, size)
                slot["exit"].update(status="OPEN", state="exit_open")
            else:
                self._submit(strategy, slot, "exit", side, size, client, market)
        self._save(strategy)

    def _emergency(self, strategy, account, market, client):
        slot = strategy["slots"][0]
        if strategy["mode"] == "paper":
            self._paper_close(strategy, self._current_price(market))
            return
        # Canceling an unfilled entry requires no quote and cannot weaken owned
        # protection. An existing native guard must survive unavailable pricing.
        if self._nonterminal(slot.get("entry")):
            self._cancel_record(strategy, slot["entry"], client, market)
            return
        fresh = client.account_snapshot()
        self._check_ownership(strategy, fresh)
        risk_market = self._risk_market(strategy, market, fresh)
        for order in (slot.get("exit"), strategy.get("protection")):
            if self._nonterminal(order):
                self._cancel_record(strategy, order, client, market)
                return
        if self._nonterminal(strategy.get("emergency")):
            return
        # Re-read after terminal cancellations; neither stale inventory nor the
        # old trade entry price may choose a market order's quantity/price bound.
        fresh = client.account_snapshot()
        self._check_ownership(strategy, fresh)
        risk_market = self._risk_market(strategy, market, fresh)
        actual = sum((self._position_size(row) for row in fresh.get("positions", [])
                      if self._matches_market(row, strategy)), ZERO)
        if actual == 0:
            return
        side = "SELL" if actual > 0 else "BUY"
        order = self._record_order(strategy, "emergency", side, self._current_price(risk_market), abs(actual), "IOC")
        order["priceReference"] = risk_market["riskPriceReference"]
        strategy["emergency"] = order
        self._save(strategy)
        try:
            ack = client.emergency_reduce(risk_market, side, string(abs(actual)), order["clientId"])
            order["orderId"] = ack.get("orderId")
        except Exception as exc:
            order.update(error=safe_error(exc), state="recovery_error")
            self._fault(strategy, "应急减仓结果未确认，保留原订单身份等待对账")
        self._save(strategy)

    def _risk_market(self, strategy, market, fresh):
        """Use public authoritative mark or a newly read owned position's mark."""
        if not fresh.get("connected", True):
            raise ValueError("应急账户快照未连接，保留已有止损")
        try:
            mark = decimal(market.get("markPrice", "0"))
        except ValueError:
            mark = ZERO
        if mark > 0:
            return {**market, "markPrice": string(mark), "riskPriceReference": "fresh_public_mark"}
        marks = []
        for row in fresh.get("positions", []):
            if self._matches_market(row, strategy) and self._position_size(row):
                try:
                    value = decimal(row.get("markPrice", row.get("markPx", "0")))
                except ValueError:
                    value = ZERO
                if value > 0:
                    marks.append(value)
        if len(marks) == 1:
            return {**market, "markPrice": string(marks[0]), "riskPriceReference": "fresh_owned_position_mark"}
        raise ValueError("公开行情与新读取持仓均无有效标记价，保留已有止损并继续对账")

    def _paper_close(self, strategy, price):
        slot = strategy["slots"][0]
        size = decimal(slot["inventory"])
        if size:
            profit = (size * price - decimal(slot["entryCost"])) * (1 if strategy["direction"] == "long" else -1)
            strategy["stats"]["grossProfit"] = string(decimal(strategy["stats"]["grossProfit"]) + profit)
            strategy["stats"]["realizedPnl"] = strategy["stats"]["grossProfit"]
            slot.update(inventory="0", entryCost="0")
        for order in (slot.get("entry"), slot.get("exit"), strategy.get("protection")):
            if self._nonterminal(order):
                order.update(status="FILLED" if order is slot.get("exit") else "CANCELED", state="terminal")
        self._save(strategy)

    def _finalize(self, strategy, client, market):
        slot = strategy["slots"][0]
        if not strategy.get("trade") or decimal(slot["inventory"]) or self._nonterminal(slot.get("entry")):
            return
        for order in (slot.get("exit"), strategy.get("protection"), strategy.get("emergency")):
            if self._nonterminal(order):
                if strategy["mode"] == "live":
                    self._cancel_record(strategy, order, client, market)
                    return
                order.update(status="CANCELED", state="terminal")
        strategy["lastTrade"] = strategy["trade"]
        strategy.update(trade=None, protection=None, emergency=None, quantity="0")
        slot.update(entry=None, exit=None, state="idle", cycle=slot["cycle"] + 1)
        strategy["stats"]["cycles"] += 1
        self._save(strategy)

    def _settle_slot(self, strategy, slot):
        # Preserve the trade, entry cost and owned order identities until native
        # protection cleanup and authoritative flatness have completed.
        if self._nonterminal(slot.get("entry")):
            slot["state"] = slot["entry"]["state"]
        elif self._nonterminal(slot.get("exit")):
            slot["state"] = slot["exit"]["state"]
        else:
            slot["state"] = "inventory" if decimal(slot["inventory"]) else "idle"

    def _breakeven(self, strategy, closed_price, quote, market):
        trade = strategy.get("trade")
        slot = strategy["slots"][0]
        inventory = decimal(slot["inventory"])
        if not trade or not inventory or trade.get("breakevenActivated") or self._nonterminal(slot.get("entry")):
            return
        entry = decimal(slot["entryCost"]) / inventory
        distance = abs(entry - decimal(trade["initialStop"]))
        direction = trade["direction"]
        if direction * (closed_price - entry) < Decimal(RULES["breakevenR"]) * distance:
            return
        accounting = strategy["accounting"]
        if strategy["mode"] == "live" and not accounting.get("known"):
            trade["breakevenPendingReason"] = "实际手续费与资金费尚未核验"
            return
        entry_ids = {slot["entry"]["clientId"], str(slot["entry"].get("orderId"))} if slot.get("entry") else set()
        fills = [fill for fill in accounting.get("fills", {}).values() if fill.get("clientId") in entry_ids
                 or str(fill.get("orderId")) in entry_ids]
        if strategy["mode"] == "live" and sum((decimal(fill.get("size", fill.get("quantity", "0"))) for fill in fills), ZERO) < inventory:
            trade["breakevenPendingReason"] = "实际入场成交记录不完整"
            return
        fees = sum((decimal(fill["fee"]) for fill in fills), ZERO)
        funding = decimal(accounting.get("tradeFunding", "0"))
        rate = decimal(market["takerFee"]) + Decimal("0.0003")
        raw = ((entry + (fees - funding) / inventory) / (1 - rate) if direction == 1 else
               (entry - (fees - funding) / inventory) / (1 + rate))
        candidate = quantize_price(raw, market, ROUND_CEILING if direction == 1 else ROUND_FLOOR)
        if candidate > 0 and direction * (candidate - decimal(trade["activeStop"])) > 0 and direction * (quote - candidate) > 0:
            trade.update(activeStop=string(candidate), breakevenActivated=True,
                         breakevenScope="server_only_initial_exchange_stop_retained")
            self._save(strategy)

    def tick(self, strategy, account, market):
        live = strategy["mode"] == "live"
        client = self.client_factory(account) if live else None
        slot = strategy["slots"][0]
        snapshot = self._snapshot(account)
        if live:
            # Protect a confirmed partial entry before canceling its residual. The
            # position-level stop automatically sizes to the actual position at trigger.
            entry = slot.get("entry")
            if self._nonterminal(entry) and not strategy.get("protection"):
                try:
                    row = client.order_status(market, entry["clientId"], entry.get("orderId"))
                    if row.get("filledSize") is not None and decimal(row["filledSize"]) > 0:
                        self._submit_protection(strategy, client, market)
                except Exception as exc:
                    self._fault(strategy, "开仓部分成交核验失败，暂停新开仓")
            self._reconcile(strategy, client, market)
            self._reconcile_extra(strategy, client, market, "protection")
            self._reconcile_extra(strategy, client, market, "emergency")
            if decimal(slot["inventory"]) > 0 and not strategy.get("protection"):
                self._submit_protection(strategy, client, market)
            if not snapshot.get("connected") or now_ms() - snapshot.get("updatedAt", 0) > 45000:
                self._fault(strategy, "账户状态未同步，暂停趋势新开仓")
            else:
                try:
                    registry = strategy["orderRegistry"]
                    server_ids = {str(row["orderId"]) for row in registry.values() if row.get("orderId")}
                    if any(self._matches_market(order, strategy) and order.get("clientId") not in registry
                           and str(order.get("orderId")) not in server_ids for order in snapshot.get("orders", [])):
                        raise ValueError("ETH-USD 出现非本策略挂单，暂停趋势新开仓")
                    last_change = strategy.get("lastInventoryChangeAt", strategy["createdAt"])
                    if snapshot.get("updatedAt", 0) >= last_change + 15000 and not self._unresolved(strategy):
                        self._check_ownership(strategy, snapshot)
                except ValueError as exc:
                    self._fault(strategy, str(exc))
            if strategy.get("status") == "configuring" or now_ms() - strategy.get("marginSetup", {}).get("verifiedAt", 0) > 15000:
                verified = self._ensure_margin(strategy, account, market, allow_write=False)
                if verified and not strategy.get("hasStarted") and strategy.get("desiredStatus") == "running":
                    fresh = client.account_snapshot()
                    fresh["updatedAt"] = fresh.get("updatedAt", fresh.get("updatedAtMs", now_ms()))
                    account["snapshot"] = snapshot = fresh
                    if not fresh.get("connected") or decimal(fresh.get("availableBalance")) < decimal(strategy["budget"]):
                        self._fault(strategy, "合约设置确认后可用保证金不足，取消启动")
                    else:
                        self._check_ownership(strategy, fresh)
                        strategy.update(status="running", hasStarted=True, suspended=False,
                                        activatedAt=strategy.get("activatedAt", now_ms()), error=None)
            if strategy.get("suspended"):
                self._cancel_owned(strategy, client, market, entries_only=True)
        try:
            price = self._current_price(market)
            strategy["currentPrice"] = string(price)
        except ValueError as exc:
            self._fault(strategy, str(exc))
            price = None
        risk_remaining = self._read_accounting(strategy, client, market, snapshot)
        if strategy.get("dayRisk", {}).get("halted") and strategy.get("trade"):
            # This flag is set only from a verified net ledger (or explicit paper
            # accounting). Preserve the proven halt if a later read fails. A new
            # unknown ledger merely blocks entry and never creates this intent.
            strategy["trade"].update(exitReason="daily_loss_limit", urgent=True,
                                     exitRequestedAt=min(strategy["trade"].get("exitRequestedAt", now_ms()),
                                                         now_ms() - RULES["stopTimeoutMs"]))
            self._save(strategy)
        fresh_readings = []
        try:
            fresh_readings = self._update_indicator(strategy, market)
        except Exception as exc:
            self._fault(strategy, safe_error(exc))
        trade = strategy.get("trade")
        if not live and price is not None and trade:
            previous = decimal(strategy.get("paperLastPrice", price))
            entry, exit_order = slot.get("entry"), slot.get("exit")
            if self._nonterminal(entry) and (previous >= decimal(entry["price"]) >= price and previous > price
                                           if entry["side"] == "BUY" else
                                           previous <= decimal(entry["price"]) <= price and previous < price):
                entry.update(status="FILLED", state="terminal", filledSize=entry["size"], accountedFill=entry["size"])
                slot.update(inventory=entry["size"], entryCost=string(decimal(entry["size"]) * decimal(entry["price"])))
                strategy["stats"]["filledOrders"] += 1
            elif self._nonterminal(exit_order) and (previous <= decimal(exit_order["price"]) <= price and previous < price
                                                   if exit_order["side"] == "SELL" else
                                                   previous >= decimal(exit_order["price"]) >= price and previous > price):
                self._paper_close(strategy, decimal(exit_order["price"]))
            if decimal(slot["inventory"]) and trade["direction"] * (price - decimal(trade["activeStop"])) <= 0:
                self._paper_close(strategy, price)
        if trade:
            entry = slot.get("entry")
            if self._nonterminal(entry) and now_ms() - entry["submittedAt"] >= RULES["entryTimeoutMs"]:
                if live:
                    self._cancel(strategy, slot, "entry", client, market)
                else:
                    entry.update(status="CANCELED", state="terminal")
            protection = strategy.get("protection")
            if price is not None and decimal(slot["inventory"]) and trade["direction"] * (
                    price - decimal(trade["activeStop"])) <= 0:
                # Initial stop is also monitored locally while its native order is
                # pending/unavailable; the existing native guard remains in place.
                self._exit(strategy, market, client, "breakeven_stop" if trade.get("breakevenActivated") else
                           "initial_stop_breached", urgent=True)
            if price is not None and trade.get("exitReason") in ("native_protection_unconfirmed", "native_protection_rejected"):
                self._exit(strategy, market, client, trade["exitReason"], urgent=True)
            if price is not None and decimal(slot["inventory"]) and trade.get("breakevenActivated") and trade["direction"] * (
                    price - decimal(trade["activeStop"])) <= 0:
                self._exit(strategy, market, client, "breakeven_stop", urgent=True)
            if protection and protection.get("firstTriggeredAt") and now_ms() - protection["firstTriggeredAt"] >= RULES["stopTimeoutMs"]:
                trade.update(exitReason="native_stop_timeout", urgent=True, exitRequestedAt=protection["firstTriggeredAt"])
            if trade.get("exitRequestedAt") and (live or price is not None) and now_ms() - trade["exitRequestedAt"] >= (
                    RULES["stopTimeoutMs"] if trade.get("urgent") else RULES["exitTimeoutMs"]):
                try:
                    self._emergency(strategy, account, market, client)
                except Exception as exc:
                    self._fault(strategy, "应急余量减仓未完成：" + safe_error(exc))
        self._finalize(strategy, client, market)
        if fresh_readings and strategy.get("trade") and price is not None:
            self._breakeven(strategy, fresh_readings[-1][1].close, price, market)
        if strategy.get("desiredStatus") in ("paused", "stopped"):
            if live:
                self._cancel_owned(strategy, client, market, entries_only=True)
            active_regular = self._nonterminal(slot.get("entry"))
            desired = strategy["desiredStatus"]
            strategy["status"] = ("pausing" if desired == "paused" else "stopping") if active_regular else desired
            self._save(strategy)
            return strategy
        for reading, bar in fresh_readings[-1:]:
            closed_at = reading.open_time_ms + HOUR_MS
            if closed_at <= strategy["lastSignalClosedAt"]:
                continue
            strategy["lastSignalClosedAt"] = closed_at
            self._save(strategy)  # Consume once, before any trade write or capacity skip.
            trade = strategy.get("trade")
            if reading.entry_signal and trade and reading.entry_signal == -trade["direction"] and price is not None:
                self._exit(strategy, market, client, "opposite_qualified_signal")
            elif (reading.entry_signal and trade is None and strategy.get("status") == "running"
                  and not strategy.get("suspended") and risk_remaining > 0 and price is not None
                  and 0 <= now_ms() - closed_at <= RULES["maxSignalAgeMs"]):
                try:
                    available = decimal(market.get("availableForStrategy", snapshot.get("availableBalance"))) if live else (
                        decimal(strategy["budget"]) + decimal(strategy["stats"]["grossProfit"]))
                    capital = min(decimal(strategy["budget"]), available)
                    plan = entry_plan(reading.entry_signal, price, decimal(reading.atr), capital, available,
                                      risk_remaining, market, strategy["leverage"])
                    if self._can_rest(plan["side"], decimal(plan["limit"]), market):
                        strategy.update(trade={**plan, "signalClosedAt": closed_at, "activeStop": plan["stop"]},
                                        direction="long" if reading.entry_signal == 1 else "short", quantity=plan["quantity"])
                        slot.update(entryPrice=plan["limit"], quantity=plan["quantity"], exitPrice="0")
                        self._save(strategy)
                        if live:
                            self._submit(strategy, slot, "entry", plan["side"], decimal(plan["quantity"]), client, market)
                        else:
                            slot["entry"] = self._record_order(strategy, "entry", plan["side"], decimal(plan["limit"]), decimal(plan["quantity"]))
                            slot["entry"].update(status="OPEN", state="entry_open")
                except ValueError as exc:
                    strategy["entrySkipped"] = str(exc)
                    self._event(strategy, "info", "趋势信号已跳过：" + str(exc))
            if strategy.get("trade") and price is not None:
                self._breakeven(strategy, bar.close, price, market)
        if price is not None:
            strategy["paperLastPrice"] = string(price)
        self._save(strategy)
        return strategy
