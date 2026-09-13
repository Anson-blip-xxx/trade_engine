"""P7-01：state 失败矩阵 golden —— Redis/file 失败/垃圾/浄空的处理。"""
import pytest


class TestStateFailureTopology:
    def test_save_swallows_redis_errors_frozen(self, pm_storage, monkeypatch):
        """_save redis 失败 → 静默吞错（P6-01/PM OBS-10 冻结）。"""
        pm = pm_storage['pm']

        def boom(key, val):
            raise RuntimeError('down')
        monkeypatch.setattr(pm, '_rset', boom)
        pm._save({'AUSDT': {'entry': 1.0}})          # 不抛

    def test_load_meta_redis_error_empty(self, pm_storage, monkeypatch):
        pm = pm_storage['pm']

        def boom(key):
            raise RuntimeError('down')
        monkeypatch.setattr(pm, '_rget', boom)
        assert pm._load_meta() == {}                # 空（不抛）

    def test_marker_delete_failure_silent(self, pm_storage, monkeypatch):
        import shared.redis_store as rs
        pm = pm_storage['pm']

        def boom(k):
            raise RuntimeError('down')
        monkeypatch.setattr(rs, 'delete', boom)
        pm._clear_closed_marker('AUSDT')            # 不抛（OBS-5）
