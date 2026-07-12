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

    v5：加入品种开关（trade_mnq / trade_mes / trade_mym），可以自由选择本次
    运行只交易哪几个品种。风险平价的权重分配、订单数/连续亏损等风控计数，
    都只会覆盖开关打开的品种，关闭的品种不订阅数据、不占用任何风险预算。

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

        # ------------------ 品种开关：控制本次运行交易哪些品种 ------------------
        self.trade_mnq = True   # Micro E-mini Nasdaq-100
        self.trade_mes = False   # Micro E-mini S&P 500
        self.trade_mym = False  # Micro E-mini Dow Jones（默认关闭，需要时改成True）

        # 每点价值（多加品种时在这里补充 ticker -> 点值 和 开关）
        self.instrument_config = {
            "MNQ": {"enabled": self.trade_mnq, "multiplier": 2.0},
            "MES": {"enabled": self.trade_mes, "multiplier": 5.0},
            "MYM": {"enabled": self.trade_mym, "multiplier": 0.5},
        }

        # v11修复：期货保证金不能使用 price*multiplier/leverage 估算。
        # QC某些期货对象的leverage会返回1，导致把期货当股票全额占用资金，
        # 在MNQ上涨后会出现 quantity=0，从而永久没有订单。
        # 这里使用近似SPAN保证金，仅用于仓位上限控制。
        self.futures_margin = {
            "MNQ": 2500,   # Micro Nasdaq-100
            "MES": 1500,   # Micro S&P500
            "MYM": 1000    # Micro Dow
        }

        # 免费/低等级账户日志配额只有10KB/天，逐笔打印很快就会被截断。
        # 默认关闭逐笔日志；调试单个具体问题时再临时打开，或用QC自带的
        # Orders/Trades面板看成交明细，不占日志配额。
        self.verbose_logging = False

        # 诊断日志：不像verbose_logging那样逐笔记录（配额扛不住完整多年回测），
        # 只记录展期/状态自愈这类关键事件 + 每月一条心跳（成交笔数、当前持仓状态摘要），
        # 用很小的日志量就能覆盖完整回测区间，方便定位"到底是哪个月开始不交易了"
        # v13 调试框架：
        # 0 = 关闭
        # 1 = 仅交易级日志
        # 2 = 完整链路日志
        self.debug_level = 1
        self.diagnostic_logging = self.debug_level > 0
        self.trade_count = 0

        # 交易时段（美东时间），避开开盘头几分钟噪音，收盘前留出平仓缓冲
        self.session_start_minutes = 9 * 60 + 35
        self.session_end_minutes   = 15 * 60 + 45

        # ------------------ 期货合约：只订阅开关打开的品种，连续合约、反向比例调整 ------------------
        self.futures = {}
        self.multiplier = {}
        for key, cfg in self.instrument_config.items():
            if not cfg["enabled"]:
                continue
            self.futures[key] = self.add_future(
                key,
                Resolution.MINUTE,
                data_normalization_mode=DataNormalizationMode.BACKWARDS_RATIO,
                data_mapping_mode=DataMappingMode.LAST_TRADING_DAY,  # 按到期日展期，不依赖持仓量数据，更稳定
                contract_depth_offset=0
            )
            self.multiplier[key] = cfg["multiplier"]

        if len(self.futures) == 0:
            raise Exception("至少需要打开一个品种的交易开关（trade_mnq / trade_mes / trade_mym）")

        for key, fut in self.futures.items():
            fut.set_filter(0, 90)  # 只考虑90天内到期的近月合约

            if self.debug_level >= 2:
                self.log(
                    f"[Future Filter] {key} "
                    f"filter=0~90 days"
                )

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
        self.mapped_symbol = {k: None for k in self.futures}

        # 持仓状态（只在订单真正Filled后才更新，见 on_order_event）
        self.position_side = {k: 0 for k in self.futures}   # 1多 / -1空 / 0空仓
        self.stop_price    = {k: None for k in self.futures}
        self.target_price  = {k: None for k in self.futures}

        # 挂单期间的待定止损/止盈距离（点数），成交后用实际成交价换算成价位
        self.pending_stop_dist   = {k: None for k in self.futures}
        self.pending_target_dist = {k: None for k in self.futures}
        self.pending_side        = {k: 0 for k in self.futures}

        # ---- 风控状态 ----
        self.entry_price       = {k: None for k in self.futures}  # 当前持仓的实际成交入场价
        self.initial_stop_dist = {k: None for k in self.futures}  # 入场时的止损距离（点数），用于计算移动止损触发点
        self.trailing_active   = {k: False for k in self.futures}
        self.trades_today      = {k: 0 for k in self.futures}      # 单品种当日开仓次数
        self.cooldown_until    = {k: None for k in self.futures}   # 止损冷却期截止时间
        self.consecutive_losses = 0                            # 单日连续亏损笔数（所有已启用品种合计）
        self.daily_orders_count = 0                             # 单日订单数（安全阀计数）

        # 实际持有仓位所在的具体合约symbol（区别于mapped_symbol——mapped_symbol每根bar
        # 都会刷新成"当前"近月合约，展期时会先于我们平仓而变化，不能用来做持仓相关判断）
        self.holding_symbol = {k: None for k in self.futures}

        self.daily_start_equity  = self.portfolio.total_portfolio_value
        self.trading_halted_today = False

        self.set_warm_up(timedelta(days=15))

        # ------------------ 日程：重置日内状态 / 收盘前强制平仓 / 每月心跳 ------------------
        self.schedule.on(self.date_rules.every_day(), self.time_rules.at(9, 30), self.reset_daily_state)
        self.schedule.on(self.date_rules.every_day(), self.time_rules.at(15, 45), self.flatten_all)
        self.schedule.on(self.date_rules.month_start(), self.time_rules.at(9, 31), self._monthly_heartbeat)

    # ------------------------------------------------------------------
    def _make_consolidation_handler(self, key: str):
        def handler(sender, bar):
            if self.atr_ind[key].is_ready:
                self.atr_window[key].add(self.atr_ind[key].current.value)
        return handler

    # ------------------------------------------------------------------
    def _monthly_heartbeat(self):
        if not self.diagnostic_logging:
            return
        status = " ".join(
            f"{k}[side={self.position_side[k]},hold={'Y' if self.holding_symbol[k] else 'N'},"
            f"mapped={'Y' if self.mapped_symbol.get(k) else 'N'},"
            f"cd={'Y' if (self.cooldown_until[k] is not None and self.time < self.cooldown_until[k]) else 'N'}]"
            for k in self.futures
        )
        self.log(f"[心跳]{self.time.date()} 总成交={self.trade_count} "
                 f"权益={self.portfolio.total_portfolio_value:.0f} halt={self.trading_halted_today} {status}")

        # 信号诊断：显示每个品种的指标就绪状态和信号被阻塞的原因
        for k in self.futures:
            ema_ready = self.ema_fast[k].is_ready and self.ema_slow[k].is_ready
            atr_ready = self.atr_ind[k].is_ready
            adx_ready = self.adx_ind[k].is_ready
            win_count = self.atr_window[k].count
            atr_val = self.atr_ind[k].current.value if atr_ready else 0
            adx_val = self.adx_ind[k].current.value if adx_ready else 0
            fast_val = self.ema_fast[k].current.value if self.ema_fast[k].is_ready else 0
            slow_val = self.ema_slow[k].current.value if self.ema_slow[k].is_ready else 0

            # 计算ATR排名
            atr_rank = -1
            if win_count >= self.atr_lookback and atr_ready:
                atr_values = sorted(self.atr_window[k])
                current_atr = self.atr_ind[k].current.value
                atr_rank = sum(1 for v in atr_values if v <= current_atr) / len(atr_values)

            sig = self._get_signal(k)
            self.log(f"[信号诊断]{self.time.date()} {k} sig={sig} "
                     f"ema_rdy={ema_ready} atr_rdy={atr_ready} adx_rdy={adx_ready} "
                     f"win={win_count}/{self.atr_lookback} "
                     f"atr={atr_val:.2f} rank={atr_rank:.2f} adx={adx_val:.1f} "
                     f"fast={fast_val:.1f} slow={slow_val:.1f}")

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
                if self.verbose_logging or self.diagnostic_logging:
                    self.log(f"[自愈]{self.time.date()} {key} 记账认为持仓,实际空仓,重置为空仓")
                self._reset_position_state(key)

            elif self.position_side[key] == 0 and holding is not None and holding in self.portfolio and self.portfolio[holding].invested:
                # 反过来：账户实际有仓位，但我们以为是空仓 -> 同样按实际持仓纠正，
                # 用当前市价当作入场价的保守估计（无法还原真实入场价，只能这样兜底）
                actual_qty = self.portfolio[holding].quantity
                if actual_qty != 0:
                    if self.verbose_logging or self.diagnostic_logging:
                        self.log(f"[自愈]{self.time.date()} {key} 记账认为空仓,实际持仓,按实际纠正")
                    self.position_side[key] = 1 if actual_qty > 0 else -1
                    self.holding_symbol[key] = holding
                    self.entry_price[key] = self.portfolio[holding].average_price
                    atr_val = self.atr_ind[key].current.value if self.atr_ind[key].is_ready else None
                    self.initial_stop_dist[key] = atr_val * self.atr_stop_mult if atr_val else None
                    self.stop_price[key] = None
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
    def on_data(self, slice):
        if self.is_warming_up:
            return

        # ---- 状态自愈：拿账户实际持仓校验我们自己维护的仓位状态 ----
        # 万一因为任何原因（保证金强平、订单竞争等未预见情况）导致内部记账和
        # 实际持仓对不上，之前的设计会永久卡死（以为有仓位、但平仓单发不出去，
        # 从此不再交易）。这里每根bar都做一次核对，发现不一致直接按实际持仓纠正，
        # 保证策略不会因为记账bug而永久停摆。
        self._reconcile_state()

        # 更新映射合约（每次rollover后 mapped 会变化）；如果当前持仓的合约不再是
        # 最新近月合约，说明发生了展期，主动平掉旧合约，而不是等摘牌被动强平
        # （被动强平那笔订单用的是旧合约symbol，容易和已经刷新的mapped_symbol对不上，
        # 导致仓位状态没法正常重置——这正是之前"展期后不再产生新订单"的根因）
        for key, fut in self.futures.items():
            new_mapped = fut.mapped
            if self.debug_level >= 2:
                self._debug_log(
                    2,
                    f"on_data [Mapped检查] {self.time} "
                    f"{key} "
                    f"mapped={new_mapped} "
                    f"inSecurities={new_mapped in self.Securities if new_mapped else False}"
                )

            if new_mapped:
                contract = self.securities[new_mapped]
                self._debug_log(
                    2,
                    f"[Mapped Contract]"
                    f"{key} "
                    f"{new_mapped} "
                    f"expiry={contract.symbol.id}"
                )
    
            old_holding = self.holding_symbol[key]

            # 诊断：mapped合约变成None，说明展期机制没能解析出下一张可交易合约，
            # 后续所有开仓都会被_has_valid_price挡住——只在状态刚变化时记一次，避免刷屏
            if new_mapped is None and self.mapped_symbol.get(key) is not None:
                if self.verbose_logging or self.diagnostic_logging:
                    self.log(f"[警告]{self.time.date()} {key} mapped合约变为None，展期解析失败")
            elif new_mapped is not None and self.mapped_symbol.get(key) is None and self.mapped_symbol.get(key) != new_mapped:
                if self.verbose_logging or self.diagnostic_logging:
                    self.log(f"[恢复]{self.time.date()} {key} mapped合约恢复为 {new_mapped.value}")

            if (self.position_side[key] != 0 and old_holding is not None
                    and new_mapped is not None and new_mapped != old_holding):
                if self._has_valid_price(old_holding):
                    self.liquidate(old_holding)
                    if self.verbose_logging or self.diagnostic_logging:
                        self.log(f"[展期]{self.time.date()} {key} 平掉旧合约 {old_holding.value}")
            self.mapped_symbol[key] = new_mapped

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
    def _debug_log(self, level: int, message: str) -> None:
        """
        v13统一调试日志入口。
        level:
        1 - 交易级
        2 - 完整诊断
        """
        if getattr(self, "debug_level", 0) >= level:
            self.log(message)

    def _rebalance(self, signals):
        # v12: 全链路诊断日志
        # 用于定位策略“不下单”的具体环节：
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
                    f"inSecurities={mapped in self.Securities}"
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
                    f"inSecurities={symbol in self.Securities} "
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
            stop_distance_points = atr_val * self.atr_stop_mult
            target_distance_points = atr_val * self.atr_target_mult
            dollar_risk_per_contract = stop_distance_points * self.multiplier[key]

            if dollar_risk_per_contract <= 0:
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

            # 诊断quantity被保证金限制的情况
            if max_affordable < 1:
                if self.debug_level >= 2:
                    self._debug_log(2,
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

            quantity = min(quantity, max_affordable)

            if quantity < 1:
                if self.debug_level >= 2:
                    self._debug_log(2,f"[最终阻断]{key} quantity={quantity}")
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
    def on_order_event(self, order_event):
        if order_event.status != OrderStatus.FILLED:
            return

        self.trade_count += 1
        self.daily_orders_count += 1
        if self.verbose_logging:
            self.log(f"成交: {order_event.symbol} {order_event.fill_quantity}@{order_event.fill_price}")

        # 找到这笔成交对应哪个品种：用canonical symbol匹配，不依赖会随展期变化的
        # mapped_symbol快照——摘牌强平单用的是旧合约symbol，这时mapped_symbol可能
        # 已经指向新合约了，用mapped_symbol查找会匹配不到，导致仓位状态卡死不重置
        key = None
        for k, fut in self.futures.items():
            if order_event.symbol.canonical == fut.symbol:
                key = k
                break
        if key is None:
            return

        fill_price = order_event.fill_price
        net_qty = self.portfolio[order_event.symbol].quantity

        if net_qty == 0:
            # 平仓成交（liquidate对应的Filled事件，含正常止损止盈/展期主动平仓/摘牌强平）
            # -> 结算盈亏，更新风控状态
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

            self._reset_position_state(key)
            return

        # 开仓成交：只有这时才正式记录持仓方向、持仓合约和止损/止盈价位
        side = self.pending_side[key]
        stop_dist = self.pending_stop_dist[key]
        target_dist = self.pending_target_dist[key]

        if side == 0 or stop_dist is None:
            return  # 理论上不该发生，兜底跳过

        self.position_side[key] = side
        self.holding_symbol[key] = order_event.symbol
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

    # ------------------------------------------------------------------
    def on_end_of_algorithm(self):
        # 只在回测结束时打一条汇总日志，不管verbose_logging开关都保留，
        # 这样即使全程静默也能知道策略到底交易了多少次
        self.log(f"回测结束，总成交笔数={self.trade_count}，最终权益={self.portfolio.total_portfolio_value:.2f}")

    def on_symbol_changed_events(self, symbol_changed_events):
        # 引擎自动回调：合约展期导致symbol映射变化时触发。纯诊断用途，
        # 用try/except兜底——哪怕这里面的字段名猜错了，也绝不能让诊断代码
        # 本身把整个回测打断（之前已经在TradeBarConsolidator上吃过一次类似的亏）。
        if not self.diagnostic_logging:
            return
        try:
            for changed_event in symbol_changed_events.values():
                old_symbol = changed_event.old_symbol
                new_symbol = changed_event.new_symbol
                self.log(f"[Mapping变化] {old_symbol} -> {new_symbol}")
                if new_symbol in self.securities:
                    security = self.securities[new_symbol]
                    self.log(f"[新合约状态] {new_symbol} price={security.price} has_data={security.has_data}")
        except Exception as ex:
            self.log(f"[Mapping变化-诊断异常] {ex}")