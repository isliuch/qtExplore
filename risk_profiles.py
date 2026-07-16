"""Account-level risk controls shared by every entry strategy."""

from copy import deepcopy


# These limits protect the account and execution pipeline. A strategy must not
# relax them; it may only add more restrictive controls of its own.
COMMON_RISK_PARAMETERS = {
    "daily_loss_limit": 0.02,  # Maximum daily loss as a fraction of equity.
    "daily_loss_limit_dollars": 300.0,  # Maximum daily loss in dollars.
    "flatten_on_daily_loss_halt": True,  # Flatten positions after daily loss halt.
    "contracts_per_order": 1,  # Target contracts for each entry order.
    "fixed_dollar_stop_loss_enabled": True,  # Use a fixed-dollar initial stop.
    "stop_loss_dollars_per_contract": 100.0,  # Fixed-dollar loss per contract.
    "stop_loss_type": "fixed",  # narrower, wider, or fixed.
    "initial_stop_order_type": "stop_limit_order",  # stop_limit_order or stop_market_order.
    "stop_limit_offset_ticks": 10,  # Limit-price buffer beyond the stop trigger.
    "fixed_dollar_trailing_enabled": True,  # Use a fixed-dollar trailing distance.
    "trailing_stop_dollars_per_contract": 100.0,  # Dollar trail per contract.
    "margin_safety_buffer": 0.5,  # Fraction of remaining margin usable for entries.
    "max_consecutive_losses": 3,  # Daily consecutive-loss limit.
    "max_daily_orders": 300,  # Daily filled-order safety limit.
    "max_contracts_per_symbol": {
        "MNQ": 5,  # Micro E-mini Nasdaq-100.
        "MES": 10,  # Micro E-mini S&P 500.
        "MYM": 10,  # Micro E-mini Dow Jones.
    },
    "futures_margin": {
        "MNQ": 2500,  # Estimated margin for Micro Nasdaq-100.
        "MES": 1500,  # Estimated margin for Micro S&P 500.
        "MYM": 1000,  # Estimated margin for Micro Dow.
    },
}


def apply_common_risk(algorithm):
    """Copy account-wide risk limits onto the QCAlgorithm instance."""
    for name, value in COMMON_RISK_PARAMETERS.items():
        setattr(algorithm, name, deepcopy(value))
