"""Account-level risk controls shared by every entry strategy."""

from copy import deepcopy


# These limits protect the account and execution pipeline. A strategy must not
# relax them; it may only add more restrictive controls of its own.
COMMON_RISK_PARAMETERS = {
    "daily_loss_limit": 0.02,          # 单日最大亏损占权益比例，触发后当日停止开新仓
    "daily_loss_limit_dollars": 300.0, # 单日最大亏损金额，触发后当日停止开新仓
    "flatten_on_daily_loss_halt": True, # 日亏损熔断时是否立即平掉已有仓位
    "contracts_per_order": 1,          # 每次开仓的目标合约数量，仍受保证金与品种上限限制
    "fixed_dollar_stop_loss_enabled": True, # 是否启用固定金额初始止损
    "stop_loss_dollars_per_contract": 100.0, # 固定金额止损时，每张合约最大亏损
    "fixed_dollar_trailing_enabled": True, # 是否启用固定金额移动止损距离
    "fixed_dollar_trailing_dollars_per_contract": 100.0,
                                                   # 每张合约固定金额移动止损距离
    "margin_safety_buffer": 0.5,       # 只使用 margin_remaining 的该比例做新仓位，留缓冲
    "max_consecutive_losses": 3,       # 单日连续亏损达到此次数，当日停止开新仓
    "max_daily_orders": 300,           # 全策略单日订单数硬上限（安全阀，远低于QC限额）
    "max_contracts_per_symbol": {
        "MNQ": 5,   # Micro E-mini Nasdaq-100
        "MES": 10,  # Micro E-mini S&P 500
        "MYM": 10,  # Micro E-mini Dow Jones
    },
    # 近似SPAN保证金，仅用于仓位上限控制。
    "futures_margin": {
        "MNQ": 2500,  # Micro Nasdaq-100
        "MES": 1500,  # Micro S&P500
        "MYM": 1000,  # Micro Dow
    },
}


def apply_common_risk(algorithm):
    """Copy account-wide risk limits onto the QCAlgorithm instance."""
    for name, value in COMMON_RISK_PARAMETERS.items():
        setattr(algorithm, name, deepcopy(value))
