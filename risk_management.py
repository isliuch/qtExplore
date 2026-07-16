# region imports
from AlgorithmImports import *
# endregion


ORDER_COUNT_CATEGORIES = (
    "entry", "initial_stop", "target", "trailing", "liquidate", "cancel"
)


def _increment_order_count(algorithm, category: str) -> None:
    counts = getattr(algorithm, "order_submission_counts", None)
    if counts is None:
        counts = {name: 0 for name in ORDER_COUNT_CATEGORIES}
        algorithm.order_submission_counts = counts
    counts[category] = counts.get(category, 0) + 1


# ------------------------------------------------------------------
def reset_daily_state(self):
    self.daily_start_equity = self.portfolio.total_portfolio_value
    self.trading_halted_today = False
    self.trades_today = {k: 0 for k in self.futures}
    self.consecutive_losses = 0
    self.daily_orders_count = 0
    self.pending_cross_entry = {k: 0 for k in self.futures}

# ------------------------------------------------------------------
def _reconcile_state(self):
    for key, fut in self.futures.items():
        holding = self.holding_symbol[key]
        actually_invested = holding is not None and holding in self.portfolio and self.portfolio[holding].invested

        if self.position_side[key] != 0 and not actually_invested:
            # 我们以为有仓位，但账户实际没有 -> 大概率是某笔平仓成交没被正确
            # 记账（比如强平/订单竞争），按实际情况纠正为空仓，避免永久卡死
            self._log_anomaly(f"reconcile_{key}",
                f"[自愈]{self.time.date()} {key} 记账认为持仓,实际空仓,重置为空仓")
            self._cancel_protective_orders(key)
            self._reset_position_state(key)

        elif self.position_side[key] == 0 and holding is not None and holding in self.portfolio and self.portfolio[holding].invested:
            # 反过来：账户实际有仓位，但我们以为是空仓 -> 同样按实际持仓纠正，
            # 用当前市价当作入场价的保守估计（无法还原真实入场价，只能这样兜底）
            actual_qty = self.portfolio[holding].quantity
            if actual_qty != 0:
                self._log_anomaly(f"reconcile_{key}",
                    f"[自愈]{self.time.date()} {key} 记账认为空仓,实际持仓,按实际纠正")
                self.position_side[key] = 1 if actual_qty > 0 else -1
                self.holding_symbol[key] = holding
                self.entry_price[key] = self.portfolio[holding].average_price
                atr_value = (
                    self.atr_ind[key].current.value
                    if self.atr_ind[key].is_ready else None
                )
                self.initial_stop_dist[key], _ = self._calculate_stop_loss(
                    key, atr_value
                )
                if self.position_side[key] == 1:
                    self.stop_price[key] = (
                        self.entry_price[key] - self.initial_stop_dist[key]
                        if self.initial_stop_dist[key] is not None else None
                    )
                else:
                    self.stop_price[key] = (
                        self.entry_price[key] + self.initial_stop_dist[key]
                        if self.initial_stop_dist[key] is not None else None
                    )
                self.target_price[key] = None
                self.trailing_active[key] = False
                self._submit_protective_orders(key)

# ------------------------------------------------------------------
def _reset_position_state(self, key: str) -> None:
    self.position_side[key] = 0
    self.stop_price[key] = None
    self.target_price[key] = None
    self.pending_side[key] = 0
    self.pending_stop_dist[key] = None
    self.pending_target_dist[key] = None
    self.entry_price[key] = None
    self.initial_stop_dist[key] = None
    self.trailing_active[key] = False
    self.holding_symbol[key] = None
    self.stop_order_ticket[key] = None
    self.target_order_ticket[key] = None

def _cancel_protective_orders(self, key: str, exclude_order_id=None) -> None:
    for tickets, label in (
        (self.stop_order_ticket, "protective stop"),
        (self.target_order_ticket, "protective target"),
    ):
        ticket = tickets.get(key)
        if ticket is not None and ticket.order_id != exclude_order_id:
            _increment_order_count(self, "cancel")
            ticket.cancel(label)
        tickets[key] = None

# ------------------------------------------------------------------
def _submit_protective_orders(self, key: str) -> None:
    """Submit OCO-like stop-market and limit exits for an open position."""
    symbol = self.holding_symbol[key]
    if symbol is None or symbol not in self.portfolio:
        return

    quantity = self.portfolio[symbol].quantity
    if quantity == 0:
        return

    exit_quantity = -quantity
    stop = self.stop_price[key]
    target = self.target_price[key]
    if stop is not None:
        if self.initial_stop_order_type == "stop_limit_order":
            if (not isinstance(self.stop_limit_offset_ticks, int)
                    or isinstance(self.stop_limit_offset_ticks, bool)
                    or self.stop_limit_offset_ticks < 0):
                raise ValueError("stop_limit_offset_ticks must be a non-negative integer")
            tick_size = float(self.securities[symbol].symbol_properties.minimum_price_variation)
            limit_offset = tick_size * self.stop_limit_offset_ticks
            limit_price = stop - limit_offset if exit_quantity < 0 else stop + limit_offset
            _increment_order_count(self, "initial_stop")
            self.stop_order_ticket[key] = self.stop_limit_order(
                symbol, exit_quantity, stop, limit_price, tag="Protective stop"
            )
        elif self.initial_stop_order_type == "stop_market_order":
            _increment_order_count(self, "initial_stop")
            self.stop_order_ticket[key] = self.stop_market_order(
                symbol, exit_quantity, stop, tag="Protective stop"
            )
        else:
            raise ValueError(
                "initial_stop_order_type must be 'stop_limit_order' or "
                f"'stop_market_order', got {self.initial_stop_order_type!r}"
            )
    if target is not None:
        _increment_order_count(self, "target")
        self.target_order_ticket[key] = self.limit_order(
            symbol, exit_quantity, target, tag="Protective target"
        )

# ------------------------------------------------------------------
def _submit_trailing_stop(self, key: str, trailing_amount: float) -> None:
    """Replace the initial stop with a native trailing-stop order."""
    symbol = self.holding_symbol[key]
    if symbol is None or symbol not in self.portfolio or trailing_amount <= 0:
        return

    quantity = self.portfolio[symbol].quantity
    if quantity == 0:
        return

    initial_stop_ticket = self.stop_order_ticket[key]
    if initial_stop_ticket is not None:
        _increment_order_count(self, "cancel")
        initial_stop_ticket.cancel("Replacing initial stop with trailing stop")

    _increment_order_count(self, "trailing")
    self.stop_order_ticket[key] = self.trailing_stop_order(
        symbol,
        -quantity,
        trailing_amount,
        False,
        tag="Protective trailing stop",
    )

# ------------------------------------------------------------------
def _in_session(self):
    t = self.time
    minutes = t.hour * 60 + t.minute
    # The end time is reserved for the scheduled flatten_all call.  Keeping it
    # exclusive prevents an on_data callback at the same timestamp from
    # reopening a position immediately after the end-of-day liquidation.
    return self.session_start_minutes <= minutes < self.session_end_minutes

# ------------------------------------------------------------------
def _has_valid_price(self, symbol) -> bool:
    return (symbol is not None
            and symbol in self.securities
            and self.securities[symbol].has_data
            and self.securities[symbol].price > 0)

# ------------------------------------------------------------------
def _calculate_stop_loss(self, key: str, atr_value=None):
    """Return (stop_distance_points, dollar_risk_per_contract).

    ``stop_loss_type`` selects the fixed-dollar/ATR combination: ``narrower``
    uses the smaller distance, ``wider`` uses the larger distance, and ``fixed``
    uses the fixed-dollar distance only. When fixed-dollar stops are disabled,
    the ATR-multiple stop remains the fallback for the combination modes.
    ``None`` is returned for both values when the selected calculation cannot
    produce a valid positive stop distance.
    """
    multiplier = self.multiplier[key]

    atr_stop_distance = (
        atr_value * self.atr_stop_mult
        if atr_value is not None and atr_value > 0 else None
    )
    fixed_stop_distance = (
        self.stop_loss_dollars_per_contract / multiplier
        if multiplier > 0 else None
    )

    if self.stop_loss_type not in ("narrower", "wider", "fixed"):
        raise ValueError(
            "stop_loss_type must be 'narrower', 'wider', or 'fixed', "
            f"got {self.stop_loss_type!r}"
        )

    if self.stop_loss_type == "fixed":
        if not self.fixed_dollar_stop_loss_enabled:
            raise ValueError(
                "fixed_dollar_stop_loss_enabled must be True when "
                "stop_loss_type is 'fixed'"
            )
        stop_distance = fixed_stop_distance
    elif not self.fixed_dollar_stop_loss_enabled:
        stop_distance = atr_stop_distance
    else:
        valid_distances = [
            distance for distance in (fixed_stop_distance, atr_stop_distance)
            if distance is not None and distance > 0
        ]
        if self.stop_loss_type == "narrower":
            stop_distance = min(valid_distances) if valid_distances else None
        else:
            stop_distance = max(valid_distances) if valid_distances else None

    dollar_risk = stop_distance * multiplier if stop_distance else None

    if stop_distance is None or stop_distance <= 0:
        return None, None
    return stop_distance, dollar_risk

# ------------------------------------------------------------------
# Kept as a behavior reference while additional strategy implementations are
# introduced. Runtime signal selection is handled by _get_signal below.
def _get_signal_legacy(self, key: str) -> int:
    """趋势 + ATR过滤 信号：1做多 -1做空 0不操作"""
    if not (self.ema_fast[key].is_ready and self.ema_slow[key].is_ready
            and self.atr_ind[key].is_ready and self.adx_ind[key].is_ready):
        return 0
    if self.atr_window[key].count < self.atr_lookback:
        return 0

    atr_values = sorted(self.atr_window[key])
    current_atr = self.atr_ind[key].current.value
    rank = sum(1 for v in atr_values if v <= current_atr) / len(atr_values)
    if rank < self.atr_low_pct or rank > self.atr_high_pct:
        return 0

    if self.adx_ind[key].current.value < self.adx_threshold:
        return 0

    fast = self.ema_fast[key].current.value
    slow = self.ema_slow[key].current.value

    if fast > slow:
        return 1
    elif fast < slow:
        return -1
    return 0

# ------------------------------------------------------------------
def _get_signal(self, key: str) -> int:
    """Ask the selected entry strategy for a long, short, or flat signal."""
    return self.active_strategy.get_signal(self, key)

# ------------------------------------------------------------------
def _rebalance(self, signals):
    if not isinstance(self.contracts_per_order, int) or isinstance(self.contracts_per_order, bool):
        raise ValueError("contracts_per_order must be a positive integer")
    if self.contracts_per_order < 1:
        raise ValueError("contracts_per_order must be a positive integer")

    # v12: 全链路诊断日志
    # 用于定位策略"不下单"的具体环节：
    # signal -> position -> filter -> risk quantity -> margin -> order
    # 注意：这行每根1分钟bar在session内都会执行一次（一天390次），只挂在
    # debug_level>=2上，debug_level=1时只保留月度心跳这类低频信息，
    # 否则多年回测几小时内日志配额就会被打满
    if self.debug_level >= 2:
        self.log(f"[REBALANCE开始] {self.time} signals={signals}")

    entry_candidates = {
        key: (signals[key] or self.pending_cross_entry[key])
        for key in self.futures
        if self.position_side[key] == 0
    }
    active = {key: side for key, side in entry_candidates.items() if side != 0}

    inv_atr = {}
    for key in self.futures:
        if self.atr_ind[key].is_ready and self.atr_ind[key].current.value > 0:
            inv_atr[key] = 1.0 / self.atr_ind[key].current.value

    total_inv_atr = sum(inv_atr.get(k, 0) for k in active) if active else 0

    for key, fut in self.futures.items():
        cross_side = signals[key]
        current_side = self.position_side[key]

        if cross_side != 0:
            self.pending_cross_entry[key] = cross_side

        if self.debug_level >= 2:
            mapped = self.mapped_symbol[key]

            self._debug_log(2,
                f"[状态] {key} signal={cross_side} "
                f"position={current_side} "
                f"mapped={mapped} "
                f"inSecurities={mapped in self.securities}"
            )

        # 信号翻转或归零 -> 平掉当前实际持有的合约（不是"当前近月合约"，
        # 展期过渡期这两者可能不是同一张合约）
        if current_side != 0:
            if cross_side == 0 or cross_side == current_side:
                continue
            holding = self.holding_symbol[key]
            if self._has_valid_price(holding):
                self._cancel_protective_orders(key)
                _increment_order_count(self, "liquidate")
                self.liquidate(holding)
            continue  # 平仓单发出后，等下一根bar再评估是否开新仓，避免同一tick平开混在一起

        target_side = self.pending_cross_entry[key]
        if target_side == 0:
            if self.debug_level >= 2:
                reason = "没有新的EMA交叉"
                self.log(f"[跳过]{key} reason={reason}")
            continue

        symbol = self.mapped_symbol[key]  # 开新仓永远用当前近月合约

        # v13.1: 展期后合约订阅状态检查
        if self.debug_level >= 2:
            self._debug_log(
                2,
                f"[交易检查]{key} "
                f"symbol={symbol} "
                f"inSecurities={symbol in self.securities} "
                f"mapped={self.mapped_symbol[key]}"
            )

        if not self._has_valid_price(symbol):
            continue

        # ---- 风控过滤：全局订单数上限 / 单品种次数上限 / 止损冷却期 ----
        if self.daily_orders_count >= self.max_daily_orders:
            continue
        if self.trades_today[key] >= self.max_trades_per_symbol_per_day:
            continue
        if self.cooldown_until[key] is not None and self.time < self.cooldown_until[key]:
            continue

        # ---- 风险平价手数（第一层：按ATR止损风险预算）----
        # Position size is fixed by contracts_per_order; retain this calculation
        # for risk diagnostics only.
        equity = self.portfolio.total_portfolio_value
        risk_dollars_total = equity * self.total_risk_budget
        weight = inv_atr[key] / total_inv_atr if total_inv_atr > 0 else 0
        risk_dollars_leg = risk_dollars_total * weight

        atr_val = self.atr_ind[key].current.value
        stop_distance_points, dollar_risk_per_contract = self._calculate_stop_loss(
            key, atr_val
        )
        target_distance_points = atr_val * self.atr_target_mult

        if dollar_risk_per_contract is None:
            continue

        risk_quantity = int(risk_dollars_leg / dollar_risk_per_contract)

        if self.debug_level >= 2:
            self._debug_log(2,
                f"[风险计算]{key} "
                f"equity={equity:.0f} "
                f"riskBudget={risk_dollars_leg:.2f} "
                f"ATR={atr_val:.2f} "
                f"contractRisk={dollar_risk_per_contract:.2f} "
                f"riskQty={risk_quantity} "
                f"requestedQty={self.contracts_per_order}"
            )

        # ---- 第二层：保证金硬约束（v11修复）----
        # 期货保证金不是名义价值/leverage。
        # 使用近似保证金避免MNQ上涨后错误计算为无法交易。
        est_margin_per_contract = self.futures_margin.get(key, 3000)

        max_affordable = int(
            (self.portfolio.margin_remaining * self.margin_safety_buffer)
            / est_margin_per_contract
        )

        # 诊断quantity被保证金限制的情况——这是之前排查了很久的关键阻断点，
        # 必须始终可见（不受debug_level限制），但要控制总量
        if max_affordable < 1:
            self._log_anomaly(f"margin_block_{key}",
                f"[仓位阻断]{self.time.date()} {key} "
                f"riskQty={risk_quantity} "
                f"marginNeed={est_margin_per_contract} "
                f"remaining={self.portfolio.margin_remaining:.0f}"
            )
            continue

        if self.debug_level >= 2:
            self._debug_log(2,
                f"[保证金计算]{key} "
                f"marginNeed={est_margin_per_contract:.0f} "
                f"remaining={self.portfolio.margin_remaining:.0f} "
                f"maxQty={max_affordable}"
            )

        quantity = min(
            self.contracts_per_order,
            max_affordable,
            self.max_contracts_per_symbol[key],
        )

        if quantity < 1:
            self._log_anomaly(
                f"final_block_{key}",
                f"[最终仓位阻断] {key} quantity={quantity}"
            )
            continue

        # 记录待成交方向和止损/止盈距离，实际价位等 on_order_event 里成交后再算
        self.pending_side[key] = target_side
        self.pending_stop_dist[key] = stop_distance_points
        self.pending_target_dist[key] = target_distance_points

        if self.debug_level >= 2:
            self._debug_log(2,
                f"[提交订单]{key} symbol={symbol} "
                f"side={target_side} quantity={quantity}"
            )

        if target_side == 1:
            _increment_order_count(self, "entry")
            self.market_order(symbol, quantity)
        else:
            _increment_order_count(self, "entry")
            self.market_order(symbol, -quantity)

        if self.verbose_logging:
            self.log(f"{key} 提交开仓 side={target_side} qty={quantity} "
                     f"权重={weight:.2%} ATR={atr_val:.2f} "
                     f"预估保证金/手={est_margin_per_contract:.0f} 剩余保证金={self.portfolio.margin_remaining:.0f}")

# ------------------------------------------------------------------
def _check_stop_target(self, key: str) -> None:
    symbol = self.holding_symbol[key]
    if self.position_side[key] == 0:
        return
    if not self._has_valid_price(symbol):
        return

    price = self.securities[symbol].price
    side = self.position_side[key]

    # ---- 移动止损：浮盈达到 trailing_activation_r 倍初始止损距离后启动 ----
    if (self.entry_price[key] is not None and self.initial_stop_dist[key]
            and self.initial_stop_dist[key] > 0):
        favorable_move = (price - self.entry_price[key]) * side
        activated_trailing = False
        if not self.trailing_active[key] and favorable_move >= self.initial_stop_dist[key] * self.trailing_activation_r:
            self.trailing_active[key] = True
            activated_trailing = True
            self.target_price[key] = None  # 启动移动止损后不再用固定止盈，让利润奔跑
            target_ticket = self.target_order_ticket[key]
            if target_ticket is not None:
                _increment_order_count(self, "cancel")
                target_ticket.cancel("Trailing stop activated")
                self.target_order_ticket[key] = None

        if activated_trailing:
            atr_val = (self.atr_ind[key].current.value if self.atr_ind[key].is_ready
                       else self.initial_stop_dist[key] / self.atr_stop_mult)
            trail_dist = atr_val * self.trailing_atr_mult
            if self.fixed_dollar_trailing_enabled:
                fixed_dollars = self.trailing_stop_dollars_per_contract
                if fixed_dollars <= 0:
                    raise ValueError(
                        "trailing_stop_dollars_per_contract must be positive "
                        "when fixed_dollar_trailing_enabled is True"
                    )
                fixed_trail_dist = fixed_dollars / self.multiplier[key]
                trail_dist = min(trail_dist, fixed_trail_dist)
            if side == 1:
                old_stop = self.stop_price[key]
                new_stop = max(old_stop, price - trail_dist) if old_stop is not None else price - trail_dist
                trailing_amount = price - new_stop
            else:
                old_stop = self.stop_price[key]
                new_stop = min(old_stop, price + trail_dist) if old_stop is not None else price + trail_dist
                trailing_amount = new_stop - price

            if trailing_amount > 0:
                self.stop_price[key] = new_stop
                self._submit_trailing_stop(key, trailing_amount)

# ------------------------------------------------------------------
def flatten_all(self):
    for key, fut in self.futures.items():
        symbol = self.holding_symbol[key]
        if symbol is not None and self.portfolio[symbol].invested:
            self._cancel_protective_orders(key)
            _increment_order_count(self, "liquidate")
            self.liquidate(symbol)
            if self.verbose_logging:
                self.log(f"{key} 收盘前强制平仓")
