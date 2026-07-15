# region imports
from AlgorithmImports import *
# endregion


# ------------------------------------------------------------------
def _make_consolidation_handler(self, key: str):
    def handler(sender, bar):
        self.active_strategy.on_consolidated_bar(self, key, bar)
    return handler

# ------------------------------------------------------------------
def _log_anomaly(self, site, message, max_count=5):
    """
    异常/关键事件日志：不管debug_level是多少都会尝试打印，但同一个site
    (日志来源标识，比如"mapped_none_MNQ")最多打印max_count次，超过就
    自动静默，只在刚达到上限那一次额外补一条"后续同类日志已抑制"的提示。
    用于：mapped合约丢失、状态自愈、展期、保证金/风控长期阻断、
    捕获到的运行时异常等——这些是"出问题了才会触发"的信号，必须始终可见，
    但又不能像per-bar诊断那样无限制刷屏。
    """
    count = self._log_site_counts.get(site, 0)
    if count >= max_count:
        return
    self._log_site_counts[site] = count + 1
    self.log(message)

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

# ------------------------------------------------------------------
def _monthly_heartbeat(self):
    # 日程本身还是每月1号触发（QC没有内置的"每季度"date_rule），
    # 这里只在季度起始月（1/4/7/10月）才真正执行，其余月份直接跳过，
    # 把日志频率从"每月"降到"每季度"，节省日志配额，留出更长的监控周期。
    # A full backtest only needs one compact account checkpoint per year.
    if self.time.month != 1:
        return
    if not self.diagnostic_logging:
        return
    status = " ".join(
        f"{k}={'L' if self.position_side[k] > 0 else 'S' if self.position_side[k] < 0 else 'flat'}"
        f",mapped={'Y' if self.mapped_symbol.get(k) else 'N'}"
        f",cd={'Y' if (self.cooldown_until[k] is not None and self.time < self.cooldown_until[k]) else 'N'}"
        for k in self.futures
    )
    self.log(f"[HB] trades={self.trade_count} "
             f"equity={self.portfolio.total_portfolio_value:.0f} "
             f"halt={self.trading_halted_today} {status}")

    # Indicator detail is useful only in a deliberately short, level-2
    # investigation. Keep annual heartbeats compact for normal backtests.
    if self.debug_level < 2 or self.strategy_name != "ema_trend":
        return

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
