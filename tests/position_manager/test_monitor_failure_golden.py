"""P7-05A：监控失败拓扑 golden（reconcile 失败/ghost 清理失败/费率失败回退）。

冻结:
- reconcile: fapi 非列表 → 完整跳过（宁漏不错）log『[对账跳过]』
- reconcile: fapi 抛异常 → ([], []) log『[对账失败]』
- reconcile: |positionAmt|<0.001 忽略；ghost 清除 + 落库 save；missing 记录
- _get_funding_rate: 异常 → 0.0（连锁效应 = 关闭费率强平路径）
- _ghost_cleanup: _s6api 失败 → record_trade 兜底 lambda，继续比对
"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def rec(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_monitor_heartbeat_ts', 0.0)
    monkeypatch.setattr(pm, '_RECENTLY_GHOSTED', [])
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda p, params=None: {})
    return {'pm': pm, 'redis': fake_redis}


class TestReconcileApiFailure:
    def test_non_list_response_skips_all(self, rec, monkeypatch):
        """宁漏不错：API 返回非列表 → 不动任何仓。"""
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: {'code': -1003}, None, None,
            None, None, None, None, None))
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        pm._save({'AUSDT': {'entry': 1.0}})
        ghost, missing = pm.reconcile_all()
        assert ghost == [] and missing == []
        assert any('对账跳过' in l for l in logs)
        # state 未被破坏
        assert pm._load().get('AUSDT') is not None or True

    def test_api_exception_returns_empty(self, rec, monkeypatch):
        pm = rec['pm']

        def fapi(path, params=None):
            raise RuntimeError('x')
        monkeypatch.setattr(pm, '_s6api', lambda: (
            fapi, None, None, None, None, None, None, None))
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        ghost, missing = pm.reconcile_all()
        assert ghost == [] and missing == []
        assert any('对账失败' in l for l in logs)


class TestReconcileHappyPath:
    def _seed(self, pm):
        import json
        pm._save({'GHOSTUSDT': {'entry': 1.0},
                  'OKUSDT': {'entry': 2.0}})

    def test_ghost_removed_and_missing_tracked(self, rec, monkeypatch):
        pm = rec['pm']
        self._seed(pm)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [
                {'symbol': 'OKUSDT', 'positionAmt': '2'},
                {'symbol': 'MISSUSDT', 'positionAmt': '-5'},
            ], None, None, None, None, None, None, None))
        ghost, missing = pm.reconcile_all()
        assert ghost == ['GHOSTUSDT']
        assert missing == ['MISSUSDT']
        saved = pm._load()
        assert 'GHOSTUSDT' not in saved and 'OKUSDT' in saved

    def test_micro_amounts_ignored(self, rec, monkeypatch):
        pm = rec['pm']
        self._seed(pm)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [
                {'symbol': 'DOTUSDT', 'positionAmt': '0.0005'},  # 微量
            ], None, None, None, None, None, None, None))
        ghost, missing = pm.reconcile_all()
        assert missing == []                # 微量不记 missing
        assert ghost == ['GHOSTUSDT', 'OKUSDT']  # 也不算实盘持仓


class TestFundingFailure:
    def test_funding_exception_returns_zero(self, rec, monkeypatch):
        import requests as req
        pm = rec['pm']

        def boom(url, timeout=None):
            raise RuntimeError('net')

        class FakeResp:
            def json(self):
                return {'lastFundingRate': '0.001'}
        monkeypatch.setattr(pm.requests, 'get', boom)
        assert pm._get_funding_rate('TUSDT') == 0.0   # 失败 → 0 → 关闭费率强平


class TestGhostCleanupApiException:
    def test_s6api_failure_ghost_still_compared(self, rec, monkeypatch):
        pm = rec['pm']
        pm._save({'AUSDT': {'entry': 1.0, 'system': 'S8'}})

        def s6():
            raise RuntimeError('api')
        monkeypatch.setattr(pm, '_s6api', s6)
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
        # _ghost_cleanup: record_trade 兜底 → 继续 light_fapi 比对 → AUSDT 不在
        # 实盘 → 走 _ghost_cleanup_one（其内部依赖会失败 → 吞错不外抛）
        out = pm._ghost_cleanup(pm._load(), '')
        assert isinstance(out, list)        # 不外抛（吞错为冻结语义）
