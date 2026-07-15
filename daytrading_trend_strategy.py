# region imports
from AlgorithmImports import *
import diagnostics
import risk_management
import risk_profiles
import strategies
# endregion

class ATRTrendRiskParityMNQMES(QCAlgorithm):
    """
    日内趋势跟踪策略：MNQ (Micro E-mini Nasdaq-100) + MES (Micro E-mini S&P 500)

    代码拆成了3个文件（QuantConnect免费账户单文件上限32KB，单文件版本超限）：
      - main.py            引擎入口：initialize / on_data / on_order_event 等
      - diagnostics.py     诊断/日志相关方法（DiagnosticsMixin）
      - risk_management.py 风控/仓位管理/信号计算（RiskManagementMixin）
    三个文件里的方法通过mixin组合进同一个类，共享同一个self，逻辑和单文件版本完全一致。

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

    v11：期货保证金不能用 price*multiplier/leverage 估算——QC某些期货对象
    的leverage会返回1，导致把期货当股票全额占用资金，MNQ上涨后quantity算
    出来是0，永久没有订单。改用近似SPAN保证金硬编码表(futures_margin)。

    v13：加入分级调试框架(debug_level 0/1/2) + 异常日志(_log_anomaly，不受
    debug_level限制但每个日志来源有独立上限，避免10KB/次的日志配额被打满)。

    使用前请确认：
      - 账户/回测环境已开通期货数据权限（CME 期货数据在 QC 云端为付费订阅）
      - 保证金估算用futures_margin近似值，不是精确的期货SPAN保证金
      - 所有参数都需要样本内/样本外回测调优
    """

    def initialize(self):
        self.set_start_date(2022, 1, 1)
        self.set_end_date(2026, 4, 11)   # 按数据源实际覆盖范围调整
        self.set_cash(50000)
        self.set_time_zone(TimeZones.NEW_YORK)
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.MARGIN)

        # Switch entry logic by changing this name. New implementations belong
        # in strategies.py and are registered in its STRATEGIES dictionary.
        self.strategy_name = "ema_trend"
        self.active_strategy = strategies.create_strategy(self.strategy_name)
        self.active_strategy.configure(self)
        risk_profiles.apply_common_risk(self)

        # Strategy parameters and strategy-specific risk controls live with
        # their strategy in strategies.py. Account-wide limits live in
        # risk_profiles.py and apply regardless of the selected strategy.

        # ------------------ 品种开关：控制本次运行交易哪些品种 ------------------
        self.trade_mnq = True   # Micro E-mini Nasdaq-100
        self.trade_mes = False  # Micro E-mini S&P 500
        self.trade_mym = False  # Micro E-mini Dow Jones（默认关闭，需要时改成True）

        # 每点价值（多加品种时在这里补充 ticker -> 点值 和 开关）
        self.instrument_config = {
            "MNQ": {"enabled": self.trade_mnq, "multiplier": 2.0},
            "MES": {"enabled": self.trade_mes, "multiplier": 5.0},
            "MYM": {"enabled": self.trade_mym, "multiplier": 0.5},
        }

        # 免费/低等级账户日志配额只有10KB/天，逐笔打印很快就会被截断。
        # 默认关闭逐笔日志；调试单个具体问题时再临时打开，或用QC自带的
        # Orders/Trades面板看成交明细，不占日志配额。
        self.verbose_logging = False

        # 诊断日志：不像verbose_logging那样逐笔记录（配额扛不住完整多年回测），
        # 只记录展期/状态自愈这类关键事件 + 每季度一条心跳（成交笔数、当前持仓状态摘要），
        # 用很小的日志量就能覆盖完整回测区间，方便定位"到底是哪个月开始不交易了"
        # v13 调试框架：
        # 0 = 关闭
        # 1 = 仅交易级日志
        # 2 = 完整链路日志
        self.debug_level = 1
        self.diagnostic_logging = self.debug_level > 0
        self.trade_count = 0

        # 异常/关键事件日志：不受debug_level限制，出问题时始终会打印，
        # 但每个日志来源(site)单独计数，达到各自上限后自动停止，避免某一处
        # 高频异常把10KB/次的日志配额全部吃光（见diagnostics.py的_log_anomaly方法）
        self._log_site_counts = {}

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
                contract_depth_offset=0,
                extended_market_hours=True
            )
            self.multiplier[key] = cfg["multiplier"]

        if len(self.futures) == 0:
            raise Exception("至少需要打开一个品种的交易开关（trade_mnq / trade_mes / trade_mym）")

        for key, fut in self.futures.items():
            fut.set_filter(0, 180)  # 只考虑180天内到期的近月合约

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

            consolidator = TradeBarConsolidator(timedelta(minutes=5))
            consolidator.data_consolidated += self._make_consolidation_handler(key)
            self.subscription_manager.add_consolidator(symbol, consolidator)
            self.consolidators[key] = consolidator

            self.active_strategy.initialize(self, key, symbol, consolidator)

        # 当前实际可交易（映射）合约
        self.mapped_symbol = {k: None for k in self.futures}

        # 记录rollover后等待行情恢复的合约
        self.pending_rollover_symbols = {}

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

        # ------------------ 日程：重置日内状态 / 收盘前强制平仓 / 季度心跳 ------------------
        self.schedule.on(self.date_rules.every_day(), self.time_rules.at(9, 30), self.reset_daily_state)
        self.schedule.on(self.date_rules.every_day(), self.time_rules.at(15, 45), self.flatten_all)
        # date_rules没有内置"每季度"规则，这里还是按月触发调度，具体的季度过滤
        # 逻辑写在_monthly_heartbeat内部（只在1/4/7/10月才真正打印）
        self.schedule.on(self.date_rules.month_start(), self.time_rules.at(9, 31), self._monthly_heartbeat)

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


        for symbol, info in list(self.pending_rollover_symbols.items()):
            # A mapping event can occur while indicators are warming up.  The
            # normal monitoring loop is intentionally skipped during warm-up,
            # so start the delay clock only on this first live data point.
            if info["start_time"] is None:
                info["start_time"] = self.time
                info["monitor_started_after_warmup"] = True

            market_state = self._rollover_market_state(symbol)
            in_securities = market_state["in_securities"]
            has_data = market_state["has_data"]
            price = market_state["price"]
            is_extended_open = market_state["is_extended_open"]

            if has_data:
                delay = self.time - info["start_time"]
                self._log_anomaly(
                    f"Rollover_Recovered_{symbol}",
                    f"[Rollover恢复] {self._format_future_contract(info['old_symbol'])} "
                    f"-> {self._format_future_contract(symbol)} "
                    f"has_data=True 等待={delay} "
                    f"price={price} 状态={market_state['label']} "
                    f"mappingTime={info['mapping_time']}"
                    f"{'（计时从warm-up结束后首根数据开始）' if info.get('monitor_started_after_warmup') else ''}",
                    max_count=1
                )
                del self.pending_rollover_symbols[symbol]

            elif (
                self.time - info["start_time"] > timedelta(minutes=10)
                and not info.get("timeout_logged", False)
            ):
                if not in_securities:
                    tag = "Rollover订阅异常"
                    detail = "新合约10分钟后仍不在Securities"
                elif is_extended_open:
                    tag = "Rollover开市等待"
                    detail = "开市超过10分钟仍 has_data=False"
                else:
                    tag = "Rollover休市等待"
                    detail = "当前不在交易时段，尚未要求数据恢复"

                self._log_anomaly(
                    f"Rollover_Wait_{symbol}",
                    f"[{tag}] {self._format_future_contract(info['old_symbol'])} "
                    f"-> {self._format_future_contract(symbol)} {detail}; "
                    f"inSecurities={in_securities} price={price} "
                    f"状态={market_state['label']}",
                    max_count=1
                )
                info["timeout_logged"] = True

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
                contract = self.Securities[new_mapped]
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
                self._log_anomaly(f"mapped_none_{key}",
                    f"[警告]{self.time.date()} {key} mapped合约变为None，展期解析失败")
            elif new_mapped is not None and self.mapped_symbol.get(key) is None and self.mapped_symbol.get(key) != new_mapped:
                self._log_anomaly(f"mapped_recover_{key}",
                    f"[恢复]{self.time.date()} {key} mapped合约恢复为 "
                    f"{self._format_future_contract(new_mapped)}")

            if (self.position_side[key] != 0 and old_holding is not None
                    and new_mapped is not None and new_mapped != old_holding):
                if self._has_valid_price(old_holding):
                    self.liquidate(old_holding)
                    self._log_anomaly(f"roll_{key}",
                        f"[展期]{self.time.date()} {key} 平掉旧合约 "
                        f"{self._format_future_contract(old_holding)}", max_count=30)
            self.mapped_symbol[key] = new_mapped

        # 日内风控：当日亏损超限 / 达到盈利目标，停止开新仓。
        # 日亏损熔断可选地立即平仓，避免已有仓位继续扩大当日亏损。
        flatten_for_daily_loss = False
        if not self.trading_halted_today and self.daily_start_equity > 0:
            daily_pnl = self.portfolio.total_portfolio_value - self.daily_start_equity
            dd = daily_pnl / self.daily_start_equity
            if daily_pnl <= -self.daily_loss_limit_dollars:
                self.trading_halted_today = True
                flatten_for_daily_loss = self.flatten_on_daily_loss_halt
                self._log_anomaly("daily_dollar_loss_halt",
                    f"[日亏损金额熔断] 日内损益=${daily_pnl:.2f} "
                    f"限额=-${self.daily_loss_limit_dollars:.2f}")
            elif dd <= -self.daily_loss_limit:
                self.trading_halted_today = True
                flatten_for_daily_loss = self.flatten_on_daily_loss_halt
                self._log_anomaly("daily_loss_halt",
                    f"[日亏损熔断] 收益={dd:.2%}")
            elif self.daily_profit_target is not None and dd >= self.daily_profit_target:
                self.trading_halted_today = True
                self._log_anomaly("daily_profit_halt",
                    f"[日盈利锁定] 收益={dd:.2%}")

        if flatten_for_daily_loss:
            self.flatten_all()
            return

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
                        self._log_anomaly("consecutive_loss_halt",
                            f"[连续亏损熔断] {key} 连亏={self.consecutive_losses}")
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
    def on_end_of_algorithm(self):
        # 只在回测结束时打一条汇总日志，不管verbose_logging开关都保留，
        # 这样即使全程静默也能知道策略到底交易了多少次
        self.log(f"回测结束，总成交笔数={self.trade_count}，最终权益={self.portfolio.total_portfolio_value:.2f}")

    # ------------------------------------------------------------------
    def _rollover_market_state(self, symbol):
        if symbol not in self.securities:
            return {
                "in_securities": False,
                "has_data": False,
                "price": None,
                "is_date_open": None,
                "is_extended_open": None,
                "label": "未订阅/不在Securities"
            }

        security = self.securities[symbol]
        hours = security.exchange.hours
        is_date_open = hours.is_date_open(
            self.time, extended_market_hours=True
        )
        is_extended_open = hours.is_open(
            self.time, extended_market_hours=True
        )

        if is_extended_open:
            label = "交易时段内"
        elif not is_date_open:
            label = "假日或整日休市"
        elif self.time.weekday() >= 5:
            label = "周末休市"
        else:
            label = "交易日盘间休市"

        return {
            "in_securities": True,
            "has_data": security.has_data,
            "price": security.price,
            "is_date_open": is_date_open,
            "is_extended_open": is_extended_open,
            "label": label
        }

    # ------------------------------------------------------------------
    def _format_future_contract(self, symbol, include_expiry: bool = True) -> str:
        if symbol is None:
            return "None"

        if isinstance(symbol, str):
            try:
                symbol = self.symbol(symbol)
            except Exception:
                return symbol

        symbol_id = getattr(symbol, "id", None)
        if symbol_id is None:
            return str(symbol)

        try:
            expiry = symbol_id.date
            value = getattr(symbol, "value", str(symbol))
            if not include_expiry:
                return value
            return f"{value}(expiry={expiry.year:04d}-{expiry.month:02d}-{expiry.day:02d})"
        except Exception:
            # Diagnostic formatting must never interrupt a backtest.
            return str(symbol)

    # ------------------------------------------------------------------
    def on_symbol_changed_events(self, symbol_changed_events):
        # 引擎自动回调：合约展期导致symbol映射变化时触发。纯诊断用途，
        # 用try/except兜底——哪怕这里面的字段名猜错了，也绝不能让诊断代码
        # 本身把整个回测打断（之前已经在TradeBarConsolidator上吃过一次类似的亏）。
        # 注意：except分支始终会打印（不受diagnostic_logging限制），因为
        # "这段诊断代码本身出异常了"这件事必须始终可见，只是用_log_anomaly
        # 控制总条数，避免万一每次展期都异常导致刷屏。
        try:
            if not self.diagnostic_logging:
                return

            for _, changed_event in symbol_changed_events.items():
                old_symbol = changed_event.old_symbol
                new_symbol = changed_event.new_symbol
                market_state = self._rollover_market_state(new_symbol)
                in_securities = market_state["in_securities"]

                if in_securities and market_state["has_data"]:
                    self.log(
                        f"[Roll] {self._format_future_contract(old_symbol, False)}"
                        f"→{self._format_future_contract(new_symbol, False)} ok"
                    )
                    continue

                self.log(
                    f"[Mapping变化] {self._format_future_contract(old_symbol)} "
                    f"-> {self._format_future_contract(new_symbol)} "
                    f"newInSecurities={in_securities} "
                    f"price={market_state['price']} has_data={market_state['has_data']} "
                    f"weekday={self.time.strftime('%a')} "
                    f"dateOpenExtended={market_state['is_date_open']} "
                    f"extendedOpen={market_state['is_extended_open']} "
                    f"状态={market_state['label']}"
                )

                # 无论是否已经进入 Securities，都开始等待监控
                self.pending_rollover_symbols[new_symbol] = {
                    # Do not include warm-up in the data-availability delay.
                    # The live on_data loop initializes this on its first bar.
                    "start_time": None if self.is_warming_up else self.time,
                    "mapping_time": self.time,
                    "old_symbol": old_symbol,
                    "timeout_logged": False,
                    "monitor_started_after_warmup": False
                }

        except Exception as ex:
            self._log_anomaly(
                "symbol_changed_exception",
                f"[Mapping变化-诊断异常] {self.time} {ex}",
                max_count=10
            )


# ------------------------------------------------------------------
# QCAlgorithm 是托管类（.NET/pythonnet），Python的多重继承在托管类上不支持
# （报错 "cannot use multiple inheritance with managed classes"），所以不能用
# mixin继承的方式拆分代码。改成：diagnostics.py / risk_management.py 里写成
# 普通的模块级函数（显式带self参数），这里用赋值的方式把它们挂到类上变成方法。
# 效果和mixin继承完全一样，只是绕开了QC这个平台限制。
ATRTrendRiskParityMNQMES._make_consolidation_handler = diagnostics._make_consolidation_handler
ATRTrendRiskParityMNQMES._log_anomaly = diagnostics._log_anomaly
ATRTrendRiskParityMNQMES._debug_log = diagnostics._debug_log
ATRTrendRiskParityMNQMES._monthly_heartbeat = diagnostics._monthly_heartbeat

ATRTrendRiskParityMNQMES.reset_daily_state = risk_management.reset_daily_state
ATRTrendRiskParityMNQMES._reconcile_state = risk_management._reconcile_state
ATRTrendRiskParityMNQMES._reset_position_state = risk_management._reset_position_state
ATRTrendRiskParityMNQMES._in_session = risk_management._in_session
ATRTrendRiskParityMNQMES._has_valid_price = risk_management._has_valid_price
ATRTrendRiskParityMNQMES._calculate_stop_loss = risk_management._calculate_stop_loss
ATRTrendRiskParityMNQMES._get_signal = risk_management._get_signal
ATRTrendRiskParityMNQMES._rebalance = risk_management._rebalance
ATRTrendRiskParityMNQMES._check_stop_target = risk_management._check_stop_target
ATRTrendRiskParityMNQMES.flatten_all = risk_management.flatten_all
