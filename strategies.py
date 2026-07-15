# region imports
from AlgorithmImports import *
# endregion


class SignalStrategy:
    """Contract for interchangeable entry-signal strategies."""

    name = None
    parameters = {}
    risk_parameters = {}

    def configure(self, algorithm):
        """Apply only this strategy's signal and trade-management settings."""
        for name, value in self.parameters.items():
            setattr(algorithm, name, value)
        for name, value in self.risk_parameters.items():
            setattr(algorithm, name, value)

    def initialize(self, algorithm, key, symbol, consolidator):
        raise NotImplementedError

    def on_consolidated_bar(self, algorithm, key, bar):
        pass

    def get_signal(self, algorithm, key):
        raise NotImplementedError


class EmaTrendStrategy(SignalStrategy):
    """The original EMA crossover with ADX and ATR-percentile filters."""

    name = "ema_trend"
    parameters = {
        "fast_period": 20,      # 快速EMA周期（基于5分钟K线）
        "slow_period": 60,      # 慢速EMA周期（基于5分钟K线）
        "atr_period": 14,       # ATR周期
        "adx_period": 14,       # ADX周期
        "adx_threshold": 20,    # 趋势强度阈值，低于此值视为盘整，不交易
        "atr_lookback": 100,    # ATR历史分位数回溯窗口（5分钟bar数）
        "atr_low_pct": 0.20,    # ATR低于历史20分位 -> 波动太小，不交易
        "atr_high_pct": 0.90,   # ATR高于历史90分位 -> 波动过大/极端行情，不交易
    }
    risk_parameters = {
        "total_risk_budget": 0.01,              # 组合总风险预算：账户权益的1%（两条腿合计）
        "atr_stop_mult": 2.0,                   # ATR 倍数止损距离 = N倍ATR
        "atr_target_mult": 3.0,                 # 止盈距离 = N倍ATR
        "max_trades_per_symbol_per_day": 4,     # 单品种每日最多开仓次数
        "loss_cooldown_minutes": 30,            # 止损离场后，同一品种冷却多久才能再开仓
        "daily_profit_target": 0.03,            # 单日盈利达到此比例后停止开新仓；设为None关闭
        "trailing_activation_r": 1.0,           # 浮盈达到N倍初始止损距离后，启动移动止损
        "trailing_atr_mult": 1.5,               # 移动止损跟踪距离 = N倍ATR
    }

    def initialize(self, algorithm, key, symbol, consolidator):
        algorithm.ema_fast[key] = ExponentialMovingAverage(algorithm.fast_period)
        algorithm.ema_slow[key] = ExponentialMovingAverage(algorithm.slow_period)
        algorithm.atr_ind[key] = AverageTrueRange(
            algorithm.atr_period, MovingAverageType.WILDERS
        )
        algorithm.adx_ind[key] = AverageDirectionalIndex(algorithm.adx_period)
        algorithm.atr_window[key] = RollingWindow[float](algorithm.atr_lookback)
        algorithm.register_indicator(symbol, algorithm.ema_fast[key], consolidator)
        algorithm.register_indicator(symbol, algorithm.ema_slow[key], consolidator)
        algorithm.register_indicator(symbol, algorithm.atr_ind[key], consolidator)
        algorithm.register_indicator(symbol, algorithm.adx_ind[key], consolidator)

    def on_consolidated_bar(self, algorithm, key, bar):
        if algorithm.atr_ind[key].is_ready:
            algorithm.atr_window[key].add(algorithm.atr_ind[key].current.value)

    def get_signal(self, algorithm, key):
        if not (algorithm.ema_fast[key].is_ready and algorithm.ema_slow[key].is_ready
                and algorithm.atr_ind[key].is_ready and algorithm.adx_ind[key].is_ready):
            return 0
        if algorithm.atr_window[key].count < algorithm.atr_lookback:
            return 0
        atr_values = sorted(algorithm.atr_window[key])
        current_atr = algorithm.atr_ind[key].current.value
        rank = sum(1 for value in atr_values if value <= current_atr) / len(atr_values)
        if rank < algorithm.atr_low_pct or rank > algorithm.atr_high_pct:
            return 0
        if algorithm.adx_ind[key].current.value < algorithm.adx_threshold:
            return 0
        fast = algorithm.ema_fast[key].current.value
        slow = algorithm.ema_slow[key].current.value
        return 1 if fast > slow else -1 if fast < slow else 0


STRATEGIES = {EmaTrendStrategy.name: EmaTrendStrategy}


def create_strategy(name):
    try:
        return STRATEGIES[name]()
    except KeyError:
        available = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"Unknown strategy {name!r}. Available strategies: {available}")
