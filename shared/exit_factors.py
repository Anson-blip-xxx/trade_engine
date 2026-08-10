"""Pure exit factors shared by the Binance position manager and backtests."""


def should_exit_on_1h_reversal(pnl_pct: float) -> bool:
    return pnl_pct >= 0


def early_loss_momentum_weak(klines: list, side: str) -> bool:
    if not klines or len(klines) < 4:
        return False
    closes = [float(row[4]) for row in klines[-4:]]
    baseline = sum(closes[:3]) / 3
    if side == 'SHORT':
        return closes[-1] >= closes[-2] and closes[-1] > baseline
    return closes[-1] <= closes[-2] and closes[-1] < baseline


def is_stagnant_profit(pnl_usdt: float, hold_min: float,
                       min_hold_min: float = 90,
                       max_profit_usdt: float = 1.0) -> bool:
    return hold_min >= min_hold_min and 0 < pnl_usdt < max_profit_usdt
