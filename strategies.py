# region imports
from AlgorithmImports import *
# endregion


class SignalStrategy:
    """Contract for interchangeable entry-signal strategies."""

    name = None

    def initialize(self, algorithm, key, symbol, consolidator):
        raise NotImplementedError

    def on_consolidated_bar(self, algorithm, key, bar):
        pass

    def get_signal(self, algorithm, key):
        raise NotImplementedError


class EmaTrendStrategy(SignalStrategy):
    """The original EMA crossover with ADX and ATR-percentile filters."""

    name = "ema_trend"

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
