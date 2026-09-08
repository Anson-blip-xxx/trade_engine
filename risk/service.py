"""Risk Service（Phase 3-04B）。

承载带 IO 的 Risk 编排逻辑：balance / used margin / Redis 状态 / 时钟
通过参数注入，Risk 核心计算来自 risk/core.py。
shared_executor 通过包装函数提供默认依赖（lazy lookup → monkeypatch 兼容）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from risk.core import AtrRiskPositionSizer, score_to_fraction

__all__ = ["calc_position_qty", "drawdown_mode", "drawdown_status"]

# DD 常量（原 shared_executor._DD_*，原样迁移，不得修改）
DD_HALF = 0.08
DD_PAUSE = 0.15
DD_RECOVERY_DELAY = 4 * 3600
DD_RECOVERY_FACTOR = 0.25
DD_RECOVERY_MAX_POSITIONS = 1
DD_RECOVERY_MAX_LOSS = 0.02
DD_RECOVERY_RETRY_DELAY = 6 * 3600


# ── calc_position_qty ────────────────────────────────────────────────────

def calc_position_qty(
    *,
    get_balance: Callable[[], float],
    get_used_margin: Callable[[dict], float],
    log_fn: Callable,
    name: str, state: dict, symbol: str, price: float,
    event_type: str, strength: int, leverage: int,
    atr_pct: float = 0, stop_pct: float = 0,
) -> float:
    """原 shared_executor.calc_position_qty 原样迁移（IO 注入）。"""
    balance = get_balance()
    pool = balance * 0.80
    used = get_used_margin(state)
    remaining = max(0, pool - used)
    alloc_pct = score_to_fraction(strength)
    position_usdt = remaining * alloc_pct
    sizer = AtrRiskPositionSizer(
        pool_budget=0.80,
        min_allocation=0.03,
        max_allocation=0.15,
        risk_per_trade=0.01,
        min_notional=10.0,
    )
    modeled_budget = sizer.budget(balance, remaining, strength, leverage, atr_pct, stop_pct)
    if atr_pct > 4:
        atr_factor = max(0.2, 4.0 / atr_pct)
        log_fn(name, f'{symbol} ATR={atr_pct:.1f}% 衰减因子={atr_factor:.2f} → ${modeled_budget:.0f}')
    if modeled_budget < position_usdt:
        log_fn(name, f'{symbol} 风险模型 ${position_usdt:.0f}→${modeled_budget:.0f} (止损{stop_pct:.1%}×{leverage}x≤1%)')
    position_usdt = modeled_budget
    position_usdt = max(position_usdt, 10.0)
    qty = position_usdt / price * leverage
    log_fn(name, f'{symbol} 余额={balance:.0f} 池={pool:.0f} 已用={used:.0f} 可用={remaining:.0f} 分配={alloc_pct:.0%} → ${position_usdt:.0f}')
    return qty


# ── drawdown 状态机 ─────────────────────────────────────────────────────

def drawdown_status(
    *,
    get_balance: Callable[[], float],
    redis_get: Callable[[str], Any],
    redis_set: Callable[[str, Any], None],
    time_fn: Callable[[], float],
) -> tuple:
    """原 shared_executor._drawdown_status 原样迁移（IO 注入）。

    返回 (factor, dd_pct)。副作用（Redis 写入）通过注入的 redis_set 原样执行。
    """
    balance = get_balance()
    if balance <= 0:
        return 1.0, 0.0
    peak = redis_get('account:peak')
    if not peak or float(peak.get('bal', 0)) < balance:
        redis_set('account:peak', {'bal': balance, 'ts': time_fn()})
        return 1.0, 0.0
    peak_bal = float(peak.get('bal', 0))
    if peak_bal <= 0:
        return 1.0, 0.0
    dd = (peak_bal - balance) / peak_bal
    if dd >= DD_PAUSE:
        pause = redis_get('account:dd_pause') or {}
        paused_at = float(pause.get('ts', 0)) if isinstance(pause, dict) else 0.0
        if paused_at <= 0:
            paused_at = time_fn()
            redis_set('account:dd_pause', {
                'ts': paused_at, 'base_balance': balance, 'loss_lock': False,
            })
        base_balance = float(pause.get('base_balance', balance)) if isinstance(pause, dict) else balance
        loss_lock = bool(pause.get('loss_lock', False)) if isinstance(pause, dict) else False
        if isinstance(pause, dict) and not pause.get('base_balance'):
            pause = dict(pause)
            pause['base_balance'] = balance
            pause['loss_lock'] = loss_lock
            redis_set('account:dd_pause', pause)
        if not loss_lock and balance <= base_balance * (1 - DD_RECOVERY_MAX_LOSS):
            lock_now = time_fn()
            redis_set('account:dd_pause', {
                'ts': lock_now, 'base_balance': balance, 'loss_lock': True,
            })
            loss_lock = True
            paused_at = lock_now
        if loss_lock:
            lock_ts = float(pause.get('ts', paused_at)) if isinstance(pause, dict) else paused_at
            if time_fn() - lock_ts < DD_RECOVERY_RETRY_DELAY:
                return 0.0, dd * 100
            redis_set('account:dd_pause', {
                'ts': time_fn(), 'base_balance': balance, 'loss_lock': False,
            })
            return 0.0, dd * 100
        if time_fn() - paused_at < DD_RECOVERY_DELAY:
            return 0.0, dd * 100
        return DD_RECOVERY_FACTOR, dd * 100
    if redis_get('account:dd_pause'):
        redis_set('account:dd_pause', {})
    if dd >= DD_HALF:
        return 0.5, dd * 100
    return 1.0, dd * 100


def drawdown_mode(factor: float) -> str:
    """原 shared_executor.drawdown_mode 原样迁移。"""
    if factor <= 0:
        return 'halt'
    if factor <= DD_RECOVERY_FACTOR:
        return 'recovery'
    if factor < 1:
        return 'reduced'
    return 'normal'
