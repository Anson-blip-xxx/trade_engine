"""Risk characterization 共享夹具（P3-03）。

隔离原则：只 mock IO（Binance 余额 API / Redis 状态）；Risk 业务函数
（calc_position_qty / _drawdown_status / drawdown_mode / sizer）走真实实现。
"""

import pytest

from strategies import shared_executor as se
from strategies.position_models import AtrRiskPositionSizer


@pytest.fixture
def risk_io(monkeypatch, fake_redis):
    """Risk 测试环境：fake Redis + 可控余额，其余真实。"""
    calls = {'balance': 4000.0, 'rset_log': []}
    monkeypatch.setattr(se, '_rget', fake_redis.get)
    monkeypatch.setattr(se, '_rset', fake_redis.set)
    monkeypatch.setattr(se, '_get_balance', lambda: calls['balance'])
    monkeypatch.setattr(se, '_calc_used_margin', lambda state: 0.0)
    return {'se': se, 'redis': fake_redis, 'calls': calls,
            'sizer': AtrRiskPositionSizer()}


def set_fake_time(ts: float):
    """固定 shared_executor 的 time.time（monkeypatch 后恢复）。"""
    return patch_time(ts)


def patch_time(monkeypatch, ts: float):
    class _FakeTime:
        @staticmethod
        def time():
            return ts
    monkeypatch.setattr(se, 'time', _FakeTime())
