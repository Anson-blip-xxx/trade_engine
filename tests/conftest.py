"""pytest 共享夹具：mock Redis / Binance API / ClickHouse，避免触碰生产环境。"""
import os
import sys
import time
from pathlib import Path

os.environ['PM_NO_WS'] = '1'  # 禁止导入 position_manager 时启动真实 WS 线程

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

import pytest


class FakeRedis:
    """进程内 Redis 内存实现，模拟 redis_store 的 get/set/delete/exists。"""

    def __init__(self):
        self.data = {}

    def get(self, key):
        import json
        raw = self.data.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    def set(self, key, data):
        import json
        self.data[key] = json.dumps(data, default=str)

    def delete(self, key):
        self.data.pop(key, None)

    def exists(self, key):
        return key in self.data


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def patch_pm(monkeypatch, fake_redis):
    """统一 patch position_manager：Redis + 日志 + s6api，并记录关键调用。"""
    from shared import position_manager as pm

    calls = {'save': [], 'record_trade': [], 'post': [], 'mark_closed': [], 'clear_closed': []}

    def fake_rget(key):
        return fake_redis.get(key)

    def fake_rset(key, data):
        fake_redis.set(key, data)

    monkeypatch.setattr(pm, '_rget', fake_rget)
    monkeypatch.setattr(pm, '_rset', fake_rset)
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    # 防真实网络：取消条件单链路用 _light_fapi_*，直接短路
    monkeypatch.setattr(pm, '_light_fapi_get', lambda path, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda path, params=None: {})

    def default_fapi_get(path, params=None):
        path = (path or '').lower()
        if 'positionrisk' in path:
            return [{'symbol': 'XUSDT', 'positionAmt': '100', 'entryPrice': '1.0'}]
        return []

    def default_fapi_post(path, params=None):
        return {'orderId': 123, 'status': 'FILLED', 'executedQty': '100'}

    def make_s6api(fapi_get=None, fapi_post=None, record_trade=None, get_symbol_info=None):
        def _s6api():
            return (
                fapi_get or default_fapi_get,
                fapi_post or default_fapi_post,
                lambda *a, **k: {},
                lambda sym: 0.0,
                get_symbol_info or (lambda sym: (6, 6)),
                lambda *a, **k: (0, 0, 0),
                lambda *a, **k: 50.0,
                record_trade or (lambda *a, **k: calls['record_trade'].append(a)),
            )
        return _s6api

    # 便捷：直接 patch 掉 _save / _mark_closed / _clear_closed_marker
    monkeypatch.setattr(pm, '_save', lambda positions: calls['save'].append(positions))
    monkeypatch.setattr(pm, '_mark_closed', lambda sym: calls['mark_closed'].append(sym))
    monkeypatch.setattr(pm, '_clear_closed_marker', lambda sym: calls['clear_closed'].append(sym))

    def set_s6api(**kw):
        monkeypatch.setattr(pm, '_s6api', make_s6api(**kw))

    set_s6api()
    return {'pm': pm, 'calls': calls, 'set_s6api': set_s6api}


@pytest.fixture
def patch_executor(monkeypatch, fake_redis):
    """统一 patch shared_executor：Redis + 余额 + 日志。"""
    from strategies import shared_executor as se

    monkeypatch.setattr(se, '_rget', fake_redis.get)
    monkeypatch.setattr(se, '_rset', fake_redis.set)
    monkeypatch.setattr(se, '_log', lambda *a, **k: None)

    def set_balance(bal):
        monkeypatch.setattr(se, '_get_balance', lambda: bal)
        monkeypatch.setattr(se, 'fapi_get',
                            lambda path, params=None: {'assets': [{'asset': 'USDT', 'walletBalance': bal}]})

    set_balance(4000)
    return {'se': se, 'set_balance': set_balance}
