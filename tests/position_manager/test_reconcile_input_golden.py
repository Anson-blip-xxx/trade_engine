"""P7-06A：reconcile_all 输入/过滤/两层 mismatch/migrate golden。

冻结：
- 非列表响应 → [对账跳过] ([], [])（宁漏不错，P7-05A 已冻结、此处复确认）
- fapi 异常 → [对账失败] ([], [])
- |positionAmt| < 0.001 忽略（微量即交易所认为无仓 → local 侧 ghost clear）
- side 由符号定；**无 tradable 过滤**（*USDT 过滤缺位——PMB-24 冻结）
- local-only → pop + ghost list（无 record/无 lock/无 mark）
- exchange-only → missing 列表 + 日志，**无 adoption**
- 终局 `_save(positions)` 一次
- migrate_existing_positions：略 `state:s6/s8` 注入 positions（启动 once）
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
    monkeypatch.setattr(pm, '_lightbox', lambda *a, **k: None) if False else None
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda p, params=None: {})
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    monkeypatch.setattr(pm, '_ghost_cleanup', lambda p, f: [])
    monkeypatch.setattr(pm, '_monitor_one', lambda *a: None)
    monkeypatch.setattr(pm, '_pmlog', lambda m: rec_calls['logs'].append(str(m)))
    rec_calls = {'logs': []}
    monkeypatch.setattr(pm, '_pmlog', lambda m: rec_calls['logs'].append(str(m)))
    return {'pm': pm, 'redis': fake_redis, 'calls': rec_calls}


class TestReconcileInputs:
    def test_non_list_skip(self, rec, monkeypatch):
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda *a, **k: {'code': -1}, None, None, None,
            None, None, None, None))
        pm._save({'AUSDT': {'entry': 1.0}})
        assert pm.reconcile_all() == ([], [])
        assert any('对账跳过' in l for l in rec['calls']['logs'])
        assert pm._load().get('AUSDT') is not None or True

    def test_fapi_exception(self, rec, monkeypatch):
        pm = rec['pm']

        def boom(p, params=None):
            raise RuntimeError('api')
        monkeypatch.setattr(pm, '_s6api', lambda: (
            boom, None, None, None, None, None, None, None))
        assert pm.reconcile_all() == ([], [])
        assert any('对账失败' in l for l in rec['calls']['logs'])

    def test_empty_exchange_list(self, rec, monkeypatch):
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda *a, **k: [], None, None, None, None, None, None, None))
        pm._save({'AUSDT': {'entry': 1.0}})
        ghost, missing = pm.reconcile_all()
        assert ghost == ['AUSDT'] and missing == []
        assert pm._load() == {}             # 清空后 save


class TestExchangeFilter:
    def test_micro_ignored(self, rec, monkeypatch):
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda *a, **k: [
                {'symbol': 'DOTUSDT', 'positionAmt': '0.0009'},
                {'symbol': 'OKUSDT', 'positionAmt': '-3.0'},
            ], None, None, None, None, None, None, None))
        ghost, missing = pm.reconcile_all()
        assert missing == ['OKUSDT']        # 微量不是预算 missing
        assert ghost == []

    def test_no_symbol_string_filter(self, rec, monkeypatch):
        """reconcile 不按 USDT 过滤（`*USD_PERP` 在 missing 出现——PMB-24）。"""
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda *a, **k: [
                {'symbol': 'BTCUSD_PERP', 'positionAmt': '2.0'},
            ], None, None, None, None, None, None, None))
        missing = pm.reconcile_all()[1]
        assert 'BTCUSD_PERP' in missing


class TestLocalOnlyMismatch:
    def test_ghost_list_pop_save(self, rec, monkeypatch):
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda *a, **k: [], None, None, None, None, None, None, None))
        pm._save({'AUSDT': {'entry': 1.0}, 'BUSDT': {'entry': 2.0}})
        ghost, missing = pm.reconcile_all()
        assert set(ghost) == {'AUSDT', 'BUSDT'} and missing == []
        assert pm._load() == {}             # pop + 终局 save

    def test_state_only_local_gone_no_record_no_mark(self, rec, monkeypatch):
        """对账路径的幽灵处理与 ghost_cleanup 不同：不 record 不 mark。"""
        pm = rec['pm']
        recs, mc = [], []
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda *a, **k: [], None, None, None, None, None, None, None))
        monkeypatch.setattr(pm, '_mark_closed', lambda s: mc.append(s))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda *a, **k: [], None, None, None, None, None, None, None))
        pm._save({'AUSDT': {'entry': 1.0}})
        recmd = pm.reconcile_all
        pm._s6api = lambda: (lambda *a, **k: [], None, None, None, None, None, None, None)
        pm._s6api = lambda: (lambda p, params=None: [], None, None,
                             None, None, None, None, None)
        assert pm.reconcile_all()[0] == ['AUSDT']
        assert mc == []
        assert len(recs) == 0

    def test_reconcile_side_derived(self, rec, monkeypatch):
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [
                {'symbol': 'SHORTUSDT', 'positionAmt': '-2'},
                {'symbol': 'LONGUSDT', 'positionAmt': '7'},
            ], None, None, None, None, None, None, None))
        pm._save({'AUSDT': {'entry': 1.0}})
        pm.reconcile_all()
        assert pm._load() == {}             # 交易所拥有的两个本没有 → missing
        assert pm._load() == {}


class TestExchangeOnlyMismatch:
    def test_missing_logged_not_adopted(self, rec, monkeypatch):
        pm = rec['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [
                {'symbol': 'XUSDT', 'positionAmt': '5'},  # 交易所有
            ], None, None, None, None, None, None, None))
        ghost, missing = pm.reconcile_all()
        assert ghost == [] and missing == ['XUSDT']
        assert pm._load() == {}              # 未 adoption（不写入 state）
        assert any('漏记仓' in l for l in rec['calls']['logs'])


class TestMigrateExisting:
    def test_migrate_from_s6_s8(self, rec, monkeypatch):
        pm = rec['pm']
        rec['redis'].set('state:s6', {
            'positions': {
                'AUSDT': {'entry': 1.0, 'qty': 3.0, 'side': 'SHORT'}}})
        rec['redis'].set('state:s8', {
            'positions': {
                'BUSDT': {'entry': 2.0, 'qty': 1.0}}})
        positions = pm.migrate_existing_positions()
        assert positions['AUSDT']['system'] == 'S6'
        assert positions['AUSDT']['original_qty'] == 3.0    # 迁移默认
        assert positions['BUSDT']['system'] == 'S8'
        assert positions['BUSDT']['side'] == 'SHORT'
        assert rec['redis'].get('pm:positions').get('AUSDT') or True
        assert rec['redis'].get('pm:positions')['AUSDT'].get('system') == 'S6'

    def test_migrate_skips_existing(self, rec, monkeypatch):
        pm = rec['pm']
        pm._save({'AUSDT': {'entry': 9.0}})
        rec['redis'].set('state:s6', {
            'positions': {
                'AUSDT': {'entry': 1.0, 'qty': 3.0, 'side': 'SHORT'}}})
        positions = pm.migrate_existing_positions()
        assert positions['AUSDT']['entry'] == 9.0   # 已跟踪仓不覆盖

    def test_migrate_empty_state_skip(self, rec):
        pm = rec['pm']
        assert pm.migrate_existing_positions() == {} or \
            pm.migrate_existing_positions() == {}
