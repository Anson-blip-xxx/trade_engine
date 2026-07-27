"""
s0_reader.py v1.1 — 各策略共享的市场状态读取模块
所有系统统一从此读取市场判断。
v1.1: 增加regime/shock_score/alts_sync/system_permissions
"""
import time, logging, sys
sys.path.insert(0, "/root/.openclaw/trade/trading_engine")
from shared.redis_store import get as _rget

STALE_THRESHOLD = 180
MIN_VERSION     = "1.0.0"

log = logging.getLogger("s0_reader")


def load_market_state() -> dict | None:
    """读取 s0 完整市场状态"""
    try:
        state = _rget('market:s0')
        if not state:
            log.warning("⚠️ market_state not found in Redis")
            return None
        age = time.time() - state.get("timestamp", 0)
        if age > STALE_THRESHOLD:
            log.warning(f"⚠️ MarketGuard stale ({age:.0f}s), fallback to local detect")
            return None
        if state.get("version", "0.0.0") < MIN_VERSION:
            log.warning(f"⚠️ MarketGuard version {state.get('version')} < {MIN_VERSION}, fallback")
            return None
        return state
    except Exception as e:
        log.warning(f"⚠️ load_market_state error: {e}")
        return None


def get_market_state(fallback_fn=None) -> str:
    """
    返回 market_state: "trend" / "range" / "risk-off"
    """
    state = load_market_state()
    if state:
        return state["market_state"]
    if fallback_fn:
        return fallback_fn()
    return "range"


def get_regime(fallback: str = "range") -> str:
    """
    返回统一regime: bull_trend / weak_bull / range / weak_bear / risk-off
    S7 5档市场状态 + S6/S8 统一使用
    """
    state = load_market_state()
    if state and "regime" in state:
        return state["regime"]
    return fallback


def get_regime_score(fallback: int = 0) -> int:
    """返回regime数值评分: -7 ~ +7"""
    state = load_market_state()
    if state and "regime_score" in state:
        return state["regime_score"]
    return fallback


def is_system_allowed(system: str) -> bool:
    """检查某系统是否被允许开仓"""
    key = f"s{system}_allowed"
    state = load_market_state()
    if state and key in state:
        return state[key]
    return True  # 保守默认


def get_btc_trend(fallback: str = "neutral") -> str:
    state = load_market_state()
    if state:
        return state.get("btc_trend", fallback)
    return fallback


def get_breadth(fallback: str = "normal") -> str:
    state = load_market_state()
    if state:
        return state.get("breadth", fallback)
    return fallback


def get_breadth_ratio(fallback: float = 0.5) -> float:
    state = load_market_state()
    if state:
        return state.get("breadth_ratio", fallback)
    return fallback


def get_shock_score(fallback: int = 0) -> int:
    """冲击分 0-10, >5=高冲击"""
    state = load_market_state()
    if state and "shock_score" in state:
        return state["shock_score"]
    return fallback


def get_risk_off(fallback: bool = False) -> bool:
    state = load_market_state()
    if state:
        return state.get("risk_off", fallback)
    return fallback
