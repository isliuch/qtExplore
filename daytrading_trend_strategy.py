# region imports
from AlgorithmImports import *
# endregion

class ATRTrendRiskParityMNQMES(QCAlgorithm):
    """
    日内趋势跟踪策略：MNQ (Micro E-mini Nasdaq-100) + MES (Micro E-mini S&P 500)

    核心逻辑：
      1. 趋势判断：快/慢 EMA 交叉方向 + ADX 趋势强度确认
      2. 波动率过滤：当前 ATR 在历史滚动窗口的分位数需落在 [低分位, 高分位] 区间，
         过低（盘整无机会）或过高（极端行情/流动性风险）都不开仓
      3. 风险平价仓位：两个品种按 ATR 反比分配风险预算，使每条腿贡献大致相等的风险，
         而不是按资金等额或合约等手数分配
      4. 日内交易：固定时间窗口内交易，收盘前强制平仓，不留隔夜仓
      5. 风控：ATR 止损/止盈，单日亏损达到阈值后当日停止开新仓

    使用前请确认：
      - 账户/回测环境已开通期货数据权限（CME 期货数据在 QC 云端为付费订阅）
      - Futures.Indices 常量名称、连续合约参数（DataNormalizationMode / DataMappingMode）
        以当前 QuantConnect/Lean 文档为准，API 会随版本迭代变化
      - 所有参数（EMA周期、ADX阈值、ATR分位数区间、风险预算、止损倍数）都需要用
        样本内/样本外回测调优，这里给的是可运行的起点，不是成品参数
    """

    def Initialize(self):
        self.SetStartDate(2022, 1, 1)
        self.SetEndDate(2024, 12, 31)
        self.SetCash(50000)
        self.SetTimeZone(TimeZones.NewYork)
        self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)

        # ------------------ 策略参数 ------------------
        self.fast_period   = 20      # 快速EMA周期
        self.slow_period   = 60      # 慢速EMA周期
        self.atr_period    = 14      # ATR周期
        self.adx_period    = 14      # ADX周期
        self.adx_threshold = 20      # 趋势强度阈值，低于此值视为盘整，不交易

        self.atr_lookback  = 100     # ATR历史分位数回溯窗口（bar数）
        self.atr_low_pct   = 0.20    # ATR低于历史20分位 -> 波动太小，不交易
        self.atr_high_pct  = 0.90    # ATR高于历史90分位 -> 波动过大/极端行情，不交易

        self.total_risk_budget = 0.01   # 组合总风险预算：账户权益的1%（两条腿合计）
        self.atr_stop_mult     = 2.0    # 止损距离 = N倍ATR
        self.atr_target_mult   = 3.0    # 止盈距离 = N倍ATR（风险回报比约1.5）
        self.daily_loss_limit  = 0.02   # 单日最大亏损占权益比例，触发后当日停止开新仓

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

        # 每点价值（用于仓位换算）
        self.multiplier = {"MNQ": 2.0, "MES": 5.0}

        # ------------------ 指标（挂在连续合约的canonical symbol上）------------------
        self.ema_fast   = {}
        self.ema_slow   = {}
        self.atr        = {}
        self.adx        = {}
        self.atr_window = {}

        for key, fut in self.futures.items():
            symbol = fut.Symbol
            self.ema_fast[key]   = self.EMA(symbol, self.fast_period, Resolution.Minute)
            self.ema_slow[key]   = self.EMA(symbol, self.slow_period, Resolution.Minute)
            self.atr[key]        = self.ATR(symbol, self.atr_period, MovingAverageType.Wilders, Resolution.Minute)
            self.adx[key]        = self.ADX(symbol, self.adx_period, Resolution.Minute)
            self.atr_window[key] = RollingWindow[float](self.atr_lookback)

        # 当前实际可交易（映射）合约
        self.mapped_symbol = {"MNQ": None, "MES": None}

        # 持仓状态
        self.position_side = {"MNQ": 0, "MES": 0}   # 1多 / -1空 / 0空仓
        self.stop_price    = {"MNQ": None, "MES": None}
        self.target_price  = {"MNQ": None, "MES": None}

        self.daily_start_equity  = self.Portfolio.TotalPortfolioValue
        self.trading_halted_today = False

        self.SetWarmUp(timedelta(days=15))

        # ------------------ 日程：重置日内状态 / 收盘前强制平仓 ------------------
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(9, 30), self.ResetDailyState)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(15, 45), self.FlattenAll)

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
    def OnData(self, slice):
        if self.IsWarmingUp:
            return

        # 更新映射合约（每次rollover后 Mapped 会变化）
        for key, fut in self.futures.items():
            self.mapped_symbol[key] = fut.Mapped

        # 日内风控：当日亏损超限，停止开新仓（但止损止盈仍然照常执行）
        if not self.trading_halted_today and self.daily_start_equity > 0:
            dd = (self.Portfolio.TotalPortfolioValue - self.daily_start_equity) / self.daily_start_equity
            if dd <= -self.daily_loss_limit:
                self.trading_halted_today = True
                self.Log(f"触发单日亏损限制 {dd:.2%}，今日停止开新仓")

        # 更新ATR滚动窗口 & 检查止损止盈（任何时段都执行，保护已有持仓）
        for key in self.futures:
            if self.atr[key].IsReady:
                self.atr_window[key].Add(self.atr[key].Current.Value)
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

        # ATR波动率过滤：剔除过低（无机会）和过高（极端行情）的波动环境
        atr_values = sorted(self.atr_window[key])
        current_atr = self.atr[key].Current.Value
        rank = sum(1 for v in atr_values if v <= current_atr) / len(atr_values)
        if rank < self.atr_low_pct or rank > self.atr_high_pct:
            return 0

        # 趋势强度过滤
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
        # ---- 风险平价：按ATR反比分配风险预算 ----
        active = {k: v for k, v in signals.items() if v != 0}

        inv_atr = {}
        for key in self.futures:
            if self.atr[key].IsReady and self.atr[key].Current.Value > 0:
                inv_atr[key] = 1.0 / self.atr[key].Current.Value

        total_inv_atr = sum(inv_atr.get(k, 0) for k in active) if active else 0

        for key, fut in self.futures.items():
            target_side = signals[key]
            symbol = self.mapped_symbol[key]
            if symbol is None:
                continue

            current_side = self.position_side[key]

            # 信号翻转或归零 -> 先平仓
            if target_side != current_side and current_side != 0:
                self.Liquidate(symbol)
                self.position_side[key] = 0
                self.stop_price[key] = None
                self.target_price[key] = None
                current_side = 0

            if target_side == 0 or current_side != 0:
                continue  # 无信号，或已持有同向仓位，不重复开仓

            # ---- 风险平价手数计算 ----
            equity = self.Portfolio.TotalPortfolioValue
            risk_dollars_total = equity * self.total_risk_budget
            weight = inv_atr[key] / total_inv_atr if total_inv_atr > 0 else 0
            risk_dollars_leg = risk_dollars_total * weight

            atr_val = self.atr[key].Current.Value
            stop_distance_points = atr_val * self.atr_stop_mult
            dollar_risk_per_contract = stop_distance_points * self.multiplier[key]

            if dollar_risk_per_contract <= 0:
                continue

            quantity = int(risk_dollars_leg / dollar_risk_per_contract)
            if quantity < 1:
                continue

            price = self.Securities[symbol].Price
            if target_side == 1:
                self.MarketOrder(symbol, quantity)
                self.stop_price[key] = price - stop_distance_points
                self.target_price[key] = price + atr_val * self.atr_target_mult
            else:
                self.MarketOrder(symbol, -quantity)
                self.stop_price[key] = price + stop_distance_points
                self.target_price[key] = price - atr_val * self.atr_target_mult

            self.position_side[key] = target_side
            self.Log(f"{key} 开仓 side={target_side} qty={quantity} "
                     f"权重={weight:.2%} ATR={atr_val:.2f} 止损={self.stop_price[key]:.2f}")

    # ------------------------------------------------------------------
    def _check_stop_target(self, key):
        symbol = self.mapped_symbol[key]
        if symbol is None or self.position_side[key] == 0:
            return
        if not self.Securities.ContainsKey(symbol):
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
            self.Log(f"{key} {reason}平仓 @ {price:.2f}")
            self.position_side[key] = 0
            self.stop_price[key] = None
            self.target_price[key] = None

    # ------------------------------------------------------------------
    def FlattenAll(self):
        for key, fut in self.futures.items():
            symbol = self.mapped_symbol[key]
            if symbol is not None and self.Portfolio[symbol].Invested:
                self.Liquidate(symbol)
                self.Log(f"{key} 收盘前强制平仓")
            self.position_side[key] = 0
            self.stop_price[key] = None
            self.target_price[key] = None

    # ------------------------------------------------------------------
    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(f"成交: {orderEvent.Symbol} {orderEvent.FillQuantity}@{orderEvent.FillPrice}")
