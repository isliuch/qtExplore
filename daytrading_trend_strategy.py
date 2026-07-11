# region imports
from AlgorithmImports import *
# endregion

class ATRTrendRiskParityMNQMES(QCAlgorithm):
    """
    日内趋势跟踪策略：MNQ (Micro E-mini Nasdaq-100) + MES (Micro E-mini S&P 500)

    v3：全面改用 QuantConnect 新版 Python API 命名规范（PEP8 下划线风格）。
    旧版 Pascal 命名（SetStartDate / AddFuture / Resolution.Minute 等）在当前
    Lean 云端IDE里已经不再兼容，必须用 set_start_date / add_future /
    Resolution.MINUTE 这种写法，枚举成员也要全大写下划线。

    v2 修复日志（针对更早一版回测报错，逻辑不变，仅命名规范更新）：
      1. [Insufficient buying power] 加入基于 Leverage 的保证金估算，
         用 margin_remaining 二次封顶手数，不再只按止损距离算风险敞口。
      2. [does not have an accurate price] 展期日新合约数据未到时，
         下单/取价前统一检查 has_data 且 price > 0。
      3. [超过10000笔订单上限] 指标改成挂在5分钟Consolidator上，
         1分钟数据只用于精确执行止损/止盈/收盘平仓。
      4. [仓位状态与实际成交不同步] 仓位状态、止损止盈价格只在
         on_order_event 里订单真正 Filled 之后才落地。
      5. [日志配额10KB/天] 逐笔日志默认关闭，只保留回测结束汇总。

    v4：加入日内交易风控层，独立于开仓信号逻辑：
      - 单品种每日开仓次数上限，防止同一品种反复进出
      - 止损离场后冷却期，避免刚被打止损又立刻反手/重新进场
      - 单日连续亏损次数熔断，达到阈值后当日停止开新仓
      - 全策略单日订单数硬上限，作为逻辑异常导致刷单的安全阀
      - 移动止损：浮盈达到设定倍数后启动，替换掉固定止盈，让趋势利润奔跑
      - 单日盈利目标（可选）：达到目标后当日停止开新仓，锁定利润

    使用前请确认：
      - 账户/回测环境已开通期货数据权限（CME 期货数据在 QC 云端为付费订阅）
      - 保证金估算用 Leverage 做近似，不是精确的期货SPAN保证金
      - 所有参数都需要样本内/样本外回测调优
    """

    def initialize(self):
        self.set_start_date(2022, 1, 1)
        self.set_end_date(2026, 4, 11)   # 按数据源实际覆盖范围调整
        self.set_cash(50000)
        self.set_time_zone(TimeZones.NEW_YORK)
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.MARGIN)

        # ------------------ 策略参数 ------------------
        self.fast_period   = 20      # 快速EMA周期（基于5分钟K线）
        self.slow_period   = 60      # 慢速EMA周期（基于5分钟K线）
        self.atr_period    = 14      # ATR周期
        self.adx_period    = 14      # ADX周期
        self.adx_threshold = 20      # 趋势强度阈值，低于此值视为盘整，不交易

        self.atr_lookback  = 100     # ATR历史分位数回溯窗口（5分钟bar数）
        self.atr_low_pct   = 0.20    # ATR低于历史20分位 -> 波动太小，不交易
        self.atr_high_pct  = 0.90    # ATR高于历史90分位 -> 波动过大/极端行情，不交易

        self.total_risk_budget = 0.01   # 组合总风险预算：账户权益的1%（两条腿合计）
        self.atr_stop_mult     = 2.0    # 止损距离 = N倍ATR
        self.atr_target_mult   = 3.0    # 止盈距离 = N倍ATR
        self.daily_loss_limit  = 0.02   # 单日最大亏损占权益比例，触发后当日停止开新仓

        self.margin_safety_buffer = 0.5  # 只使用 margin_remaining 的这个比例做新仓位，留缓冲

        # ------------------ 日内交易风控参数 ------------------
        self.max_trades_per_symbol_per_day = 4     # 单品种每日最多开仓次数
        self.loss_cooldown_minutes         = 30    # 止损离场后，同一品种冷却这么久才能再开仓
        self.max_consecutive_losses        = 3     # 单日连续亏损次数达到此值，当日停止开新仓
        self.max_daily_orders              = 300   # 全策略单日订单数硬上限（安全阀，远低于QC限额）
        self.daily_profit_target           = 0.03  # 单日盈利达到此比例后停止开新仓；设为 None 关闭该功能
        self.trailing_activation_r         = 1.0   # 浮盈达到N倍初始止损距离后，启动移动止损
        self.trailing_atr_mult             = 1.5   # 移动止损跟踪距离 = N倍ATR

        # 免费/低等级账户日志配额只有10KB/天，逐笔打印很快就会被截断。
        # 默认关闭逐笔日志；调试单个具体问题时再临时打开，或用QC自带的
        # Orders/Trades面板看成交明细，不占日志配额。
        self.verbose_logging = False
        self.trade_count = 0

        # 交易时段（美东时间），避开开盘头几分钟噪音，收盘前留出平仓缓冲
        self.session_start_minutes = 9 * 60 + 35
        self.session_end_minutes   = 15 * 60 + 45

        # ------------------ 期货合约：连续合约，反向比例调整，便于计算指标 ------------------
        self.futures = {}
        self.futures["MNQ"] = self.add_future(
            "MNQ",
            Resolution.MINUTE,
            data_normalization_mode=DataNormalizationMode.BACKWARDS_RATIO,
            data_mapping_mode=DataMappingMode.OPEN_INTEREST,
            contract_depth_offset=0
        )
        self.futures["MES"] = self.add_future(
            "MES",
            Resolution.MINUTE,
            data_normalization_mode=DataNormalizationMode.BACKWARDS_RATIO,
            data_mapping_mode=DataMappingMode.OPEN_INTEREST,
            contract_depth_offset=0
        )

        for fut in self.futures.values():
            fut.set_filter(0, 90)  # 只考虑90天内到期的近月合约

        # 每点价值
        self.multiplier = {"MNQ": 2.0, "MES": 5.0}

        # ------------------ 指标：挂在5分钟Consolidator上，减少信号噪音 ------------------
        self.ema_fast   = {}
        self.ema_slow   = {}
        self.atr_ind    = {}
        self.adx_ind    = {}
        self.atr_window = {}
        self.consolidators = {}

        for key, fut in self.futures.items():
            symbol = fut.symbol

            self.ema_fast[key]   = ExponentialMovingAverage(self.fast_period)
            self.ema_slow[key]   = ExponentialMovingAverage(self.slow_period)
            self.atr_ind[key]        = AverageTrueRange(self.atr_period, MovingAverageType.WILDERS)
            self.adx_ind[key]        = AverageDirectionalIndex(self.adx_period)
            self.atr_window[key] = RollingWindow[float](self.atr_lookback)

            consolidator = TradeBarConsolidator(timedelta(minutes=5))
            consolidator.data_consolidated += self._make_consolidation_handler(key)
            self.subscription_manager.add_consolidator(symbol, consolidator)
            self.consolidators[key] = consolidator

            self.register_indicator(symbol, self.ema_fast[key], consolidator)
            self.register_indicator(symbol, self.ema_slow[key], consolidator)
            self.register_indicator(symbol, self.atr_ind[key], consolidator)
            self.register_indicator(symbol, self.adx_ind[key], consolidator)

        # 当前实际可交易（映射）合约
        self.mapped_symbol = {"MNQ": None, "MES": None}

        # 持仓状态（只在订单真正Filled后才更新，见 on_order_event）
        self.position_side = {"MNQ": 0, "MES": 0}   # 1多 / -1空 / 0空仓
        self.stop_price    = {"MNQ": None, "MES": None}
        self.target_price  = {"MNQ": None, "MES": None}

        # 挂单期间的待定止损/止盈距离（点数），成交后用实际成交价换算成价位
        self.pending_stop_dist   = {"MNQ": None, "MES": None}
        self.pending_target_dist = {"MNQ": None, "MES": None}
        self.pending_side        = {"MNQ": 0, "MES": 0}

        # ---- 风控状态 ----
        self.entry_price       = {"MNQ": None, "MES": None}  # 当前持仓的实际成交入场价
        self.initial_stop_dist = {"MNQ": None, "MES": None}  # 入场时的止损距离（点数），用于计算移动止损触发点
        self.trailing_active   = {"MNQ": False, "MES": False}
        self.trades_today      = {"MNQ": 0, "MES": 0}         # 单品种当日开仓次数
        self.cooldown_until    = {"MNQ": None, "MES": None}   # 止损冷却期截止时间
        self.consecutive_losses = 0                            # 单日连续亏损笔数（两品种合计）
        self.daily_orders_count = 0                             # 单日订单数（安全阀计数）

        self.daily_start_equity  = self.portfolio.total_portfolio_value
        self.trading_halted_today = False

        self.set_warm_up(timedelta(days=15))

        # ------------------ 日程：重置日内状态 / 收盘前强制平仓 ------------------
        self.schedule.on(self.date_rules.every_day(), self.time_rules.at(9, 30), self.reset_daily_state)
        self.schedule.on(self.date_rules.every_day(), self.time_rules.at(15, 45), self.flatten_all)

    # ------------------------------------------------------------------
    def _make_consolidation_handler(self, key):
        def handler(sender, bar):
            if self.atr_ind[key].is_ready:
                self.atr_window[key].add(self.atr_ind[key].current.value)
        return handler

    # ------------------------------------------------------------------
    def reset_daily_state(self):
        self.daily_start_equity = self.portfolio.total_portfolio_value
        self.trading_halted_today = False
        self.trades_today = {"MNQ": 0, "MES": 0}
        self.consecutive_losses = 0
        self.daily_orders_count = 0

    # ------------------------------------------------------------------
    def _in_session(self):
        t = self.time
        minutes = t.hour * 60 + t.minute
        return self.session_start_minutes <= minutes <= self.session_end_minutes

    # ------------------------------------------------------------------
    def _has_valid_price(self, symbol):
        return (symbol is not None
                and symbol in self.securities
                and self.securities[symbol].has_data
                and self.securities[symbol].price > 0)

    # ------------------------------------------------------------------
    def on_data(self, slice):
        if self.is_warming_up:
            return

        # 更新映射合约（每次rollover后 mapped 会变化）
        for key, fut in self.futures.items():
            self.mapped_symbol[key] = fut.mapped

        # 日内风控：当日亏损超限 / 达到盈利目标，停止开新仓（止损止盈仍照常执行）
        if not self.trading_halted_today and self.daily_start_equity > 0:
            dd = (self.portfolio.total_portfolio_value - self.daily_start_equity) / self.daily_start_equity
            if dd <= -self.daily_loss_limit:
                self.trading_halted_today = True
                if self.verbose_logging:
                    self.log(f"触发单日亏损限制 {dd:.2%}，今日停止开新仓")
            elif self.daily_profit_target is not None and dd >= self.daily_profit_target:
                self.trading_halted_today = True
                if self.verbose_logging:
                    self.log(f"达到单日盈利目标 {dd:.2%}，今日停止开新仓，锁定利润")

        # 止损止盈检查，任何时段都执行，保护已有持仓
        for key in self.futures:
            self._check_stop_target(key)

        # 只在设定交易时段内开新仓
        if not self._in_session():
            return

        signals = {key: self._get_signal(key) for key in self.futures}

        if not self.trading_halted_today:
            self._rebalance(signals)

    # ------------------------------------------------------------------
    def _get_signal(self, key):
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
        active = {k: v for k, v in signals.items() if v != 0}

        inv_atr = {}
        for key in self.futures:
            if self.atr_ind[key].is_ready and self.atr_ind[key].current.value > 0:
                inv_atr[key] = 1.0 / self.atr_ind[key].current.value

        total_inv_atr = sum(inv_atr.get(k, 0) for k in active) if active else 0

        for key, fut in self.futures.items():
            target_side = signals[key]
            symbol = self.mapped_symbol[key]

            if not self._has_valid_price(symbol):
                continue

            current_side = self.position_side[key]

            # 信号翻转或归零 -> 先平仓
            if target_side != current_side and current_side != 0:
                self.liquidate(symbol)
                continue  # 平仓单发出后，等下一根bar再评估是否开新仓，避免同一tick平开混在一起

            if target_side == 0 or current_side != 0:
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
            stop_distance_points = atr_val * self.atr_stop_mult
            target_distance_points = atr_val * self.atr_target_mult
            dollar_risk_per_contract = stop_distance_points * self.multiplier[key]

            if dollar_risk_per_contract <= 0:
                continue

            quantity = int(risk_dollars_leg / dollar_risk_per_contract)

            # ---- 第二层：保证金硬约束，防止风险预算算出账户扛不住的手数 ----
            price = self.securities[symbol].price
            leverage = max(self.securities[symbol].leverage, 1.0)
            notional_per_contract = price * self.multiplier[key]
            est_margin_per_contract = notional_per_contract / leverage

            if est_margin_per_contract <= 0:
                continue

            max_affordable = int((self.portfolio.margin_remaining * self.margin_safety_buffer)
                                  / est_margin_per_contract)
            quantity = min(quantity, max_affordable)

            if quantity < 1:
                continue

            # 记录待成交方向和止损/止盈距离，实际价位等 on_order_event 里成交后再算
            self.pending_side[key] = target_side
            self.pending_stop_dist[key] = stop_distance_points
            self.pending_target_dist[key] = target_distance_points

            if target_side == 1:
                self.market_order(symbol, quantity)
            else:
                self.market_order(symbol, -quantity)

            if self.verbose_logging:
                self.log(f"{key} 提交开仓 side={target_side} qty={quantity} "
                         f"权重={weight:.2%} ATR={atr_val:.2f} "
                         f"预估保证金/手={est_margin_per_contract:.0f} 剩余保证金={self.portfolio.margin_remaining:.0f}")

    # ------------------------------------------------------------------
    def on_order_event(self, order_event):
        if order_event.status != OrderStatus.FILLED:
            return

        self.trade_count += 1
        self.daily_orders_count += 1
        if self.verbose_logging:
            self.log(f"成交: {order_event.symbol} {order_event.fill_quantity}@{order_event.fill_price}")

        # 找到这笔成交对应哪个品种
        key = None
        for k, sym in self.mapped_symbol.items():
            if sym is not None and sym == order_event.symbol:
                key = k
                break
        if key is None:
            return

        fill_price = order_event.fill_price
        net_qty = self.portfolio[order_event.symbol].quantity

        if net_qty == 0:
            # 平仓成交（liquidate对应的Filled事件）-> 结算盈亏，更新风控状态
            closed_side = self.position_side[key]
            if self.entry_price[key] is not None and closed_side != 0:
                pnl_points = (fill_price - self.entry_price[key]) * closed_side
                if pnl_points < 0:
                    self.consecutive_losses += 1
                    self.cooldown_until[key] = self.time + timedelta(minutes=self.loss_cooldown_minutes)
                    if self.consecutive_losses >= self.max_consecutive_losses:
                        self.trading_halted_today = True
                        if self.verbose_logging:
                            self.log(f"单日连续亏损达到{self.consecutive_losses}笔，今日停止开新仓")
                else:
                    self.consecutive_losses = 0

            self.position_side[key] = 0
            self.stop_price[key] = None
            self.target_price[key] = None
            self.pending_side[key] = 0
            self.pending_stop_dist[key] = None
            self.pending_target_dist[key] = None
            self.entry_price[key] = None
            self.initial_stop_dist[key] = None
            self.trailing_active[key] = False
            return

        # 开仓成交：只有这时才正式记录持仓方向和止损/止盈价位
        side = self.pending_side[key]
        stop_dist = self.pending_stop_dist[key]
        target_dist = self.pending_target_dist[key]

        if side == 0 or stop_dist is None:
            return  # 理论上不该发生，兜底跳过

        self.position_side[key] = side
        self.entry_price[key] = fill_price
        self.initial_stop_dist[key] = stop_dist
        self.trailing_active[key] = False
        self.trades_today[key] += 1
        if side == 1:
            self.stop_price[key] = fill_price - stop_dist
            self.target_price[key] = fill_price + target_dist
        else:
            self.stop_price[key] = fill_price + stop_dist
            self.target_price[key] = fill_price - target_dist

        self.pending_side[key] = 0
        self.pending_stop_dist[key] = None
        self.pending_target_dist[key] = None

    # ------------------------------------------------------------------
    def _check_stop_target(self, key):
        symbol = self.mapped_symbol[key]
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
            symbol = self.mapped_symbol[key]
            if symbol is not None and self.portfolio[symbol].invested:
                self.liquidate(symbol)
                if self.verbose_logging:
                    self.log(f"{key} 收盘前强制平仓")

    # ------------------------------------------------------------------
    def on_end_of_algorithm(self):
        # 只在回测结束时打一条汇总日志，不管verbose_logging开关都保留，
        # 这样即使全程静默也能知道策略到底交易了多少次
        self.log(f"回测结束，总成交笔数={self.trade_count}，最终权益={self.portfolio.total_portfolio_value:.2f}")