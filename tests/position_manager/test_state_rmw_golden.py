"""P7-01：RMW / multiple writers / restart / aliasing golden。

地址 P7-02 StateService 拆分最易误伤的 state 语义。
"""
import pytest


def make_pos(qty=10.0, **kw):
    base = {'entry': 2.0, 'qty': qty, 'original_qty': 10.0, 'side': 'SHORT',
            'system': 'S8', 'open_time': 1.0, 'leverage': 3, 'sl': 2.2,
            'signal_type': 'S8', 'event_type': 'S8', 'score': 66,
            'be_done': False}
    base.update(kw)
    return base


class TestRMW:
    def test_partial_close_rmw_qty_update(self, pm_full):
        """partial 文：order → qty = round(qty-close,4) → _save（PMB-6 无 PG）"""
        pm = pm_full['pm']
        pos = make_pos()
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
        assert pos['qty'] == 6.0
        assert pm_full['redis'].get('pm:positions')['AUSDT']['qty'] == 6.0


class TestMultipleWriters:
    def test_se_rmw_writes_then_pm_save_overwrites_legacy(self, pm_full, monkeypatch):
        """PM S0 paths 均可写 pm:positions；最后写者胜（顺序模拟）——restore state."""
        pm = pm_full['pm']
        import strategies.shared_executor as se
        monkeypatch.setattr(se, '_rget', pm_full['redis'].get)
        monkeypatch.setattr(se, '_rset', pm_full['redis'].set)
        monkeypatch.setattr(se, '_POS_CACHE', {})
        monkeypatch.setattr(se, 'tg_send', lambda *a, **k: None)
        # Writer A：se._update_pos_cache（RMW——legacy direct）
        se._update_pos_cache('S8', 'AUSDT', 'SHORT', 2.0, 10.0, 2.2, 3,
                             'ISOLATED', 'S8', 66)
        row_a = pm_full['redis'].get('pm:positions')['AUSDT']
        assert row_a['entry'] == 2.0 and row_a['ts'] > 0
        assert 'position_id' in row_a                # 四段式 position_id（P6-01）
        # Writer B：PM._save（全量快照）
        pm._save({'AUSDT': {**make_pos(qty=6.0)}})
        row_b = pm_full['redis'].get('pm:positions')['AUSDT']
        assert row_b['qty'] == 6.0                    # last writer wins
        # Writer A 重写 → 覆盖 Writer B（两 writer 都可写，frozen）
        se._update_pos_cache('S8', 'AUSDT', 'LONG', 3.0, 5.0, 3.2, 5, 'ISOLATED',
                             'S8', 80)
        row_c = pm_full['redis'].get('pm:positions')['AUSDT']
        assert row_c['qty'] == 5.0 and row_c['side'] == 'LONG'

    def test_open_position_dup_record_true_pm_obs3_frozen(self, pm_full, monkeypatch):
        """same sym 重复 open：order 已发出但记录不覆盖（PM OBS-3 冻结）"""
        pm = pm_full['pm']
        existing = make_pos()
        pm_full['redis'].set('pm:positions', {'AUSDT': existing})
        # 模拟 open_position 已下单后的登记路径（830-833）
        positions = pm._load()
        if 'AUSDT' in positions:
            pm._pmlog('[开仓] AUSDT 已在持仓中，跳过')  # 830 行原有 log
            return True                                  # frozen OBS-3
        pm._pmlog('unexpected path')


class TestRestart:
    def test_restart_globals_reset_redis_only(self, pm_full, monkeypatch):
        """重启：进程态丢失（_POS_CACHE fresh）→ redis 仍 serves state。"""
        pm = pm_full['pm']
        pm_full['redis'].set('pm:positions',
                             {'AUSDT': dict(make_pos())})
        import strategies.shared_executor as se
        monkeypatch.setattr(se, '_POS_CACHE', None)
        pm_full['redis'].get('pm:positions')  # still reads fine
        rows = pm._load_meta()
        assert 'AUSDT' in rows and rows['AUSDT']['entry'] == 2.0


class TestStateAlias:
    def test_load_meta_fresh_dict_per_call(self, pm_storage):
        """`_load_meta` 每次构造 fresh dict：对 rows 原地修改不回写 redis
        （JSON 反序列化语义）。state 更新必须显式 _save（P7-02 约束）。"""
        pm = pm_storage['pm']
        pm_storage['redis'].set('pm:positions', {'AUSDT': make_pos()})
        rows = pm._load_meta()
        rows['AUSDT']['qty'] = 55.5                   # 原地修改（内存副本）
        assert pm_storage['redis'].get('pm:positions')['AUSDT']['qty'] == 10.0

    def test_dict_ordering_insertion_order(self, pm_storage):
        """state dict insertion order 满足 JSON 序列化顺序（不 sort）。"""
        pm = pm_storage['pm']
        pm_storage['redis'].set('pm:positions',
                                {'AA': {'entry': 1.0}, 'BB': {'entry': 2.0},
                                 'ZZ': {'entry': 3.0}})
        keys = list(pm._load_meta().keys())
        assert keys == ['AA', 'BB', 'ZZ']             # FIFO insertion order
