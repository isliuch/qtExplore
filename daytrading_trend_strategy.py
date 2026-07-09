# region imports
from AlgorithmImports import *
# endregion

class ATRTrendRiskParityMNQMES(QCAlgorithm):
    """
    日内趋势跟踪策略：MNQ (Micro E-mini Nasdaq-100) + MES (Micro E-mini S&P 500)

    v2 修复日志（针对第一版回测报错）：
      1. [Insufficient buying power] 原来的仓位公式只按"风险预算/止损距离"算手数，
         完全没检查保证金够不够，导致下出账户根本扛不住的巨大仓位。
         -> 加入基于 Leverage 的保证金估算，用 MarginRemaining 二次封顶手数。
      2. [security does not have an accurate price] 每季度合约展期(3/6/9/12月)那天，
         Mapped 合约切换瞬间新合约可能还没有数据，直接用来下单/取价会报错。
         -> 下单和取价前都检查 HasData 且 Price > 0。
      3. [超过10000笔订单上限] 原来直接在1分钟频率上算EMA/ADX/ATR，信号噪音大、
         换仓过于频繁。
         -> 指标改成挂在5分钟K线合成器(Consolidator)上，1分钟数据只用于精确执行
            止损/止盈/收盘平仓，显著降低下单频率。
      4. [仓位状态与实际成交不同步] 原来下单后立刻假设成交并记录持仓方向/止损价，
         一旦订单被拒绝（比如报错1），仓位状态和实际持仓就对不上，后续逻辑会
         错误地认为"已有仓位"从而跳过应有的交易。
         -> 仓位状态、止损止盈价格改为在 OnOrderEvent 里等订单真正 Filled 之后才写入。

    使用前请确认：
      - 账户/回测环境已开通期货数据权限（CME 期货数据在 QC 云端为付费订阅）
      - 免费/低等级账户有订单数量上限，长期回测+高频调仓容易触发，必要时降频或升级账户
      - 保证金估算用的是 Security.Leverage 做近似，不是精确的期货SPAN保证金，
        实盘/精细回测建议换成券商真实保证金数据
      - 所有参数都需要样本内/样本外回测调优
    """

    def Initialize(self):
        self.SetStartDate(2022, 1, 1)
      	self.SetEndDate(2026, 7, 9)   # 按需调整；不设EndDate的话QC默认跑到"今天"
        self.SetCash(50000)
        self.SetTimeZone(TimeZones.NewYork)
        self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)

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

        self.margin_safety_buffer = 0.5  # 只使用 MarginRemaining 的这个比例做新仓位，留缓冲

        # 交易时段（美东时间），避开开盘头几分钟噪音，收盘前留出平仓缓冲
        self.session_start_minutes = 9 * 60 + 35
        self.session_end_minutes   = 15 * 60 + 45

        # ------------------ 期货合约：连续合约，反向比例调整，便于计算指标 ------------------
        self.futures = {}
        self.futures["MNQ"] = self.AddFuture(
            "MNQ",
            Resolution.Minute,
            dataNormalizationMode=DataNormalizationMode.BackwardsRatio,
            dataMappingMode=DataMappingMode.OpenInterest,
            contractDepthOffset=0
        )
        self.futures["MES"] = self.AddFuture(
            "MES",
            Resolution.Minute,
            dataNormalizationMode=DataNormalizationMode.BackwardsRatio,
            dataMappingMode=DataMappingMode.OpenInterest,
            contractDepthOffset=0
        )

        for fut in self.futures.values():
            fut.SetFilter(0, 90)  # 只考虑90天内到期的近月合约

        # 每点价值
        self.multiplier = {"MNQ": 2.0, "MES": 5.0}

        # ------------------ 指标：挂在5分钟Consolidator上，减少信号噪音 ------------------
        self.ema_fast   = {}
        self.ema_slow   = {}
        self.atr        = {}
        self.adx        = {}
        self.atr_window = {}
        self.consolidators = {}

        for key, fut in self.futures.items():
            symbol = fut.Symbol

            self.ema_fast[key]   = ExponentialMovingAverage(self.fast_period)
            self.ema_slow[key]   = ExponentialMovingAverage(self.slow_period)
            self.atr[key]        = AverageTrueRange(self.atr_period, MovingAverageType.Wilders)
            self.adx[key]        = AverageDirectionalIndex(self.adx_period)
            self.atr_window[key] = RollingWindow[float](self.atr_lookback)

            consolidator = TradeBarConsolidator(timedelta(minutes=5))
            consolidator.DataConsolidated += self._make_consolidation_handler(key)
            self.SubscriptionManager.AddConsolidator(symbol, consolidator)
            self.consolidators[key] = consolidator

            self.RegisterIndicator(symbol, self.ema_fast[key], consolidator)
            self.RegisterIndicator(symbol, self.ema_slow[key], consolidator)
            self.RegisterIndicator(symbol, self.atr[key], consolidator)
            self.RegisterIndicator(symbol, self.adx[key], consolidator)

        # 当前实际可交易（映射）合约
        self.mapped_symbol = {"MNQ": None, "MES": None}

        # 持仓状态（只在订单真正Filled后才更新，见 OnOrderEvent）
        self.position_side = {"MNQ": 0, "MES": 0}   # 1多 / -1空 / 0空仓
        self.stop_price    = {"MNQ": None, "MES": None}
        self.target_price  = {"MNQ": None, "MES": None}

        # 挂单期间的待定止损/止盈距离（点数），成交后用实际成交价换算成价位
        self.pending_stop_dist   = {"MNQ": None, "MES": None}
        self.pending_target_dist = {"MNQ": None, "MES": None}
        self.pending_side        = {"MNQ": 0, "MES": 0}

        self.daily_start_equity  = self.Portfolio.TotalPortfolioValue
        self.trading_halted_today = False

        self.SetWarmUp(timedelta(days=15))

        # ------------------ 日程：重置日内状态 / 收盘前强制平仓 ------------------
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(9, 30), self.ResetDailyState)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(15, 45), self.FlattenAll)

    # ------------------------------------------------------------------
    def _make_consolidation_handler(self, key):
        def handler(sender, bar):
            if self.atr[key].IsReady:
                self.atr_window[key].Add(self.atr[key].Current.Value)
        return handler

    # ------------------------------------------------------------------
    def ResetDailyState(self):
        self.daily_start_equity = self.Portfolio.TotalPortfolioValue
        self.trading_halted_today = False

    # ------------------------------------------------------------------
    def _in_session(self):
        t = self.Time
        minutes = t.hour * 60 + t.minute
        return self.session_start_minutes <= minutes <= self.session_end_minutes

    # ------------------------------------------------------------------
    def _has_valid_price(self, symbol):
        return (symbol is not None
                and self.Securities.ContainsKey(symbol)
                and self.Securities[symbol].HasData
                and self.Securities[symbol].Price > 0)

    # ------------------------------------------------------------------
    def OnData(self, slice):
        if self.IsWarmingUp:
            return

        # 更新映射合约（每次rollover后 Mapped 会变化）
        for key, fut in self.futures.items():
            self.mapped_symbol[key] = fut.Mapped

        # 日内风控：当日亏损超限，停止开新仓（止损止盈仍照常执行）
        if not self.trading_halted_today and self.daily_start_equity > 0:
            dd = (self.Portfolio.TotalPortfolioValue - self.daily_start_equity) / self.daily_start_equity
            if dd <= -self.daily_loss_limit:
                self.trading_halted_today = True
                self.Log(f"触发单日亏损限制 {dd:.2%}，今日停止开新仓")

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
        if not (self.ema_fast[key].IsReady and self.ema_slow[key].IsReady
                and self.atr[key].IsReady and self.adx[key].IsReady):
            return 0
        if self.atr_window[key].Count < self.atr_lookback:
            return 0

        atr_values = sorted(self.atr_window[key])
        current_atr = self.atr[key].Current.Value
        rank = sum(1 for v in atr_values if v <= current_atr) / len(atr_values)
        if rank < self.atr_low_pct or rank > self.atr_high_pct:
            return 0

        if self.adx[key].Current.Value < self.adx_threshold:
            return 0

        fast = self.ema_fast[key].Current.Value
        slow = self.ema_slow[key].Current.Value

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
            if self.atr[key].IsReady and self.atr[key].Current.Value > 0:
                inv_atr[key] = 1.0 / self.atr[key].Current.Value

        total_inv_atr = sum(inv_atr.get(k, 0) for k in active) if active else 0

        for key, fut in self.futures.items():
            target_side = signals[key]
            symbol = self.mapped_symbol[key]

            if not self._has_valid_price(symbol):
                continue

            current_side = self.position_side[key]

            # 信号翻转或归零 -> 先平仓
            if target_side != current_side and current_side != 0:
                self.Liquidate(symbol)
                continue  # 平仓单发出后，等下一根bar再评估是否开新仓，避免同一tick平开混在一起

            if target_side == 0 or current_side != 0:
                continue

            # ---- 风险平价手数（第一层：按ATR止损风险预算）----
            equity = self.Portfolio.TotalPortfolioValue
            risk_dollars_total = equity * self.total_risk_budget
            weight = inv_atr[key] / total_inv_atr if total_inv_atr > 0 else 0
            risk_dollars_leg = risk_dollars_total * weight

            atr_val = self.atr[key].Current.Value
            stop_distance_points = atr_val * self.atr_stop_mult
            target_distance_points = atr_val * self.atr_target_mult
            dollar_risk_per_contract = stop_distance_points * self.multiplier[key]

            if dollar_risk_per_contract <= 0:
                continue

            quantity = int(risk_dollars_leg / dollar_risk_per_contract)

            # ---- 第二层：保证金硬约束，防止风险预算算出账户扛不住的手数 ----
            price = self.Securities[symbol].Price
            leverage = max(self.Securities[symbol].Leverage, 1.0)
            notional_per_contract = price * self.multiplier[key]
            est_margin_per_contract = notional_per_contract / leverage

            if est_margin_per_contract <= 0:
                continue

            max_affordable = int((self.Portfolio.MarginRemaining * self.margin_safety_buffer)
                                  / est_margin_per_contract)
            quantity = min(quantity, max_affordable)

            if quantity < 1:
                continue

            # 记录待成交方向和止损/止盈距离，实际价位等 OnOrderEvent 里成交后再算
            self.pending_side[key] = target_side
            self.pending_stop_dist[key] = stop_distance_points
            self.pending_target_dist[key] = target_distance_points

            if target_side == 1:
                self.MarketOrder(symbol, quantity)
            else:
                self.MarketOrder(symbol, -quantity)

            self.Log(f"{key} 提交开仓 side={target_side} qty={quantity} "
                     f"权重={weight:.2%} ATR={atr_val:.2f} "
                     f"预估保证金/手={est_margin_per_contract:.0f} 剩余保证金={self.Portfolio.MarginRemaining:.0f}")

    # ------------------------------------------------------------------
    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status != OrderStatus.Filled:
            return

        self.Log(f"成交: {orderEvent.Symbol} {orderEvent.FillQuantity}@{orderEvent.FillPrice}")

        # 找到这笔成交对应哪个品种
        key = None
        for k, sym in self.mapped_symbol.items():
            if sym is not None and sym == orderEvent.Symbol:
                key = k
                break
        if key is None:
            return

        fill_price = orderEvent.FillPrice
        net_qty = self.Portfolio[orderEvent.Symbol].Quantity

        if net_qty == 0:
            # 平仓成交（Liquidate对应的Filled事件）
            self.position_side[key] = 0
            self.stop_price[key] = None
            self.target_price[key] = None
            self.pending_side[key] = 0
            self.pending_stop_dist[key] = None
            self.pending_target_dist[key] = None
            return

        # 开仓成交：只有这时才正式记录持仓方向和止损/止盈价位
        side = self.pending_side[key]
        stop_dist = self.pending_stop_dist[key]
        target_dist = self.pending_target_dist[key]

        if side == 0 or stop_dist is None:
            return  # 理论上不该发生，兜底跳过

        self.position_side[key] = side
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

        price = self.Securities[symbol].Price
        side = self.position_side[key]
        stop = self.stop_price[key]
        target = self.target_price[key]

        hit_stop = stop is not None and ((side == 1 and price <= stop) or (side == -1 and price >= stop))
        hit_target = target is not None and ((side == 1 and price >= target) or (side == -1 and price <= target))

        if hit_stop or hit_target:
            self.Liquidate(symbol)
            reason = "止损" if hit_stop else "止盈"
            self.Log(f"{key} 提交{reason}平仓 @ {price:.2f}")

    # ------------------------------------------------------------------
    def FlattenAll(self):
        for key, fut in self.futures.items():
            symbol = self.mapped_symbol[key]
            if symbol is not None and self.Portfolio[symbol].Invested:
                self.Liquidate(symbol)
                self.Log(f"{key} 收盘前强制平仓")
