# region imports
from AlgorithmImports import *
# endregion


# ------------------------------------------------------------------
def reset_daily_state(self):
    self.daily_start_equity = self.portfolio.total_portfolio_value
    self.trading_halted_today = False
    self.trades_today = {k: 0 for k in self.futures}
    self.consecutive_losses = 0
    self.daily_orders_count = 0

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

# ------------------------------------------------------------------
def _in_session(self):
    t = self.time
    minutes = t.hour * 60 + t.minute
    return self.session_start_minutes <= minutes <= self.session_end_minutes

# ------------------------------------------------------------------
def _has_valid_price(self, symbol) -> bool:
    return (symbol is not None
            and symbol in self.securities
            and self.securities[symbol].has_data
            and self.securities[symbol].price > 0)

# ------------------------------------------------------------------
def _calculate_stop_loss(self, key: str, atr_value=None):
    """Return (stop_distance_points, dollar_risk_per_contract).

    Set ``stop_loss_mode`` to ``fixed_dollar`` for a constant dollar loss per
    contract, or to ``atr`` to use the strategy's original ATR-multiple stop.
    ``None`` is returned for both values when the selected calculation cannot
    produce a valid positive stop distance.
    """
    multiplier = self.multiplier[key]

    if self.stop_loss_mode == "fixed_dollar":
        dollar_risk = self.stop_loss_dollars_per_contract
        stop_distance = dollar_risk / multiplier if multiplier > 0 else None
    elif self.stop_loss_mode == "atr":
        stop_distance = (
            atr_value * self.atr_stop_mult
            if atr_value is not None and atr_value > 0 else None
        )
        dollar_risk = stop_distance * multiplier if stop_distance else None
    else:
        raise ValueError(
            "stop_loss_mode must be 'fixed_dollar' or 'atr', "
            f"got {self.stop_loss_mode!r}"
        )

    if stop_distance is None or stop_distance <= 0:
        return None, None
    return stop_distance, dollar_risk

# ------------------------------------------------------------------
def _get_signal(self, key: str) -> int:
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
def _rebalance(self, signals):
    # v12: 全链路诊断日志
    # 用于定位策略"不下单"的具体环节：
    # signal -> position -> filter -> risk quantity -> margin -> order
    # 注意：这行每根1分钟bar在session内都会执行一次（一天390次），只挂在
    # debug_level>=2上，debug_level=1时只保留月度心跳这类低频信息，
    # 否则多年回测几小时内日志配额就会被打满
    if self.debug_level >= 2:
        self.log(f"[REBALANCE开始] {self.time} signals={signals}")

    active = {k: v for k, v in signals.items() if v != 0}

    inv_atr = {}
    for key in self.futures:
        if self.atr_ind[key].is_ready and self.atr_ind[key].current.value > 0:
            inv_atr[key] = 1.0 / self.atr_ind[key].current.value

    total_inv_atr = sum(inv_atr.get(k, 0) for k in active) if active else 0

    for key, fut in self.futures.items():
        target_side = signals[key]
        current_side = self.position_side[key]

        if self.debug_level >= 2:
            mapped = self.mapped_symbol[key]

            self._debug_log(2,
                f"[状态] {key} signal={target_side} "
                f"position={current_side} "
                f"mapped={mapped} "
                f"inSecurities={mapped in self.securities}"
            )

        # 信号翻转或归零 -> 平掉当前实际持有的合约（不是"当前近月合约"，
        # 展期过渡期这两者可能不是同一张合约）
        if target_side != current_side and current_side != 0:
            holding = self.holding_symbol[key]
            if self._has_valid_price(holding):
                self.liquidate(holding)
            continue  # 平仓单发出后，等下一根bar再评估是否开新仓，避免同一tick平开混在一起

        if target_side == 0 or current_side != 0:
            if self.debug_level >= 2:
                reason = "无信号" if target_side == 0 else "已有持仓"
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

        quantity = int(risk_dollars_leg / dollar_risk_per_contract)

        if self.debug_level >= 2:
            self._debug_log(2,
                f"[风险计算]{key} "
                f"equity={equity:.0f} "
                f"riskBudget={risk_dollars_leg:.2f} "
                f"ATR={atr_val:.2f} "
                f"contractRisk={dollar_risk_per_contract:.2f} "
                f"rawQty={quantity}"
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
                f"riskQty={quantity} "
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
            quantity,
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
            self.market_order(symbol, quantity)
        else:
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
        if not self.trailing_active[key] and favorable_move >= self.initial_stop_dist[key] * self.trailing_activation_r:
            self.trailing_active[key] = True
            self.target_price[key] = None  # 启动移动止损后不再用固定止盈，让利润奔跑

        if self.trailing_active[key]:
            atr_val = (self.atr_ind[key].current.value if self.atr_ind[key].is_ready
                       else self.initial_stop_dist[key] / self.atr_stop_mult)
            trail_dist = atr_val * self.trailing_atr_mult
            if side == 1:
                new_stop = price - trail_dist
                if self.stop_price[key] is None or new_stop > self.stop_price[key]:
                    self.stop_price[key] = new_stop
            else:
                new_stop = price + trail_dist
                if self.stop_price[key] is None or new_stop < self.stop_price[key]:
                    self.stop_price[key] = new_stop

    stop = self.stop_price[key]
    target = self.target_price[key]

    hit_stop = stop is not None and ((side == 1 and price <= stop) or (side == -1 and price >= stop))
    hit_target = target is not None and ((side == 1 and price >= target) or (side == -1 and price <= target))

    if hit_stop or hit_target:
        self.liquidate(symbol)
        if self.verbose_logging:
            reason = "止损" if hit_stop else "止盈"
            self.log(f"{key} 提交{reason}平仓 @ {price:.2f}")

# ------------------------------------------------------------------
def flatten_all(self):
    for key, fut in self.futures.items():
        symbol = self.holding_symbol[key]
        if symbol is not None and self.portfolio[symbol].invested:
            self.liquidate(symbol)
            if self.verbose_logging:
                self.log(f"{key} 收盘前强制平仓")
