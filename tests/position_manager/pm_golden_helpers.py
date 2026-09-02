"""PM Golden 快照与构造辅助（只提取当前真实存在且有业务意义的字段）。"""
from shared import position_manager as pm

POSITION_FIELDS = ('entry', 'qty', 'original_qty', 'leverage', 'sl', 'side', 'system',
                   'signal_type', 'score', 'margin_type', 'be_done', 'tp_done',
                   'highest', 'lowest', 'algo_sl_id', 'position_id', 'open_time',
                   'atr', 'event_type', 'strength', 'ts', 'stop', 'trail',
                   'trend_reversal_warned')


def position_snapshot(pos: dict) -> dict:
    """确定性快照：PM 持仓记录的当前真实字段（不新增、不推测）。"""
    return {k: pos.get(k) for k in POSITION_FIELDS if k in pos}


def make_position(**overrides) -> dict:
    """按 PM.open_position 的真实字段模板构造一个持仓记录。"""
    pos = {
        'entry': 1.0, 'qty': 100.0, 'original_qty': 100.0,
        'leverage': 3, 'sl': 0.92,
        'open_time': 1788000000, 'atr': 0.0,
        'side': 'LONG', 'system': 'S6',
        'signal_type': 'TREND_UP', 'score': 66,
        'margin_type': 'CROSSED',
        'be_done': False, 'tp_done': [],
        'highest': 1.0, 'lowest': 1.0,
        'algo_sl_id': None,
    }
    pos.update(overrides)
    return pos


def seed_positions(redis, positions: dict):
    """直接写入 pm:positions（绕过三层加载，模拟已持久化状态）。"""
    current = redis.get('pm:positions') or {}
    current.update(positions)
    redis.set('pm:positions', current)


def recorded_symbol(rec) -> str:
    return rec['args'][0]


def recorded_entry(rec) -> float:
    return rec['args'][1]


def recorded_price(rec) -> float:
    return rec['args'][2]


def recorded_qty(rec) -> float:
    return rec['args'][3]


def recorded_exit_reason(rec) -> str:
    return rec['kwargs'].get('exit_reason', '')


def recorded_side(rec) -> str:
    return rec['kwargs'].get('side', '')


def recorded_signal_type(rec) -> str:
    return rec['kwargs'].get('signal_type', '')


def cfg_by_name(name: str) -> dict:
    return pm.SYSTEM_CFG[name]
