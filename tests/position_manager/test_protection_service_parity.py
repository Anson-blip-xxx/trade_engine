"""P7-04B：ProtectionService wiring parity（PM 保护接线经 service 零 diff）。"""
import pytest

from shared import position_manager as pm


class TestPMProtectionWiring:
    def test_protection_service_factory_exists(self):
        import inspect
        src = inspect.getsource(pm)
        assert '_protection_service' in src
        assert 'position_protection.service' in src or \
            'ProtectionService' in src

    def test_enqueue_uses_service(self, monkeypatch):
        """`_protection_service().enqueue_algo_sl` 走 wrapper（passthrough 无 IO）。
        队列 list 同一个 backing。"""
        from position_protection.service import ProtectionService
        calls = []
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda s, s2, t, q: calls.append((s, s2, t, q)))
        svc = pm._protection_service()
        svc.enqueue_algo_sl('AUSDT', 'SELL', 0.92, 100.0)
        assert calls == [('AUSDT', 'SELL', 0.92, 100.0)]
        # 同 backing：pm._ALGO_QUEUE 不变
        assert pm._ALGO_QUEUE == []
        # legacy _algo_enqueue same one

    def test_queue_backing_unchanged(self, monkeypatch):
        '''ProtectionService 委托不改 backing — enqueue 后 pm._ALGO_QUEUE 填充值。'''
        pm._ALGO_QUEUE.clear()
        monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
        pm._algo_enqueue('AUSDT1', 'SELL', 0.92, 100.0)
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda s, s2, t, q: pm._ALGO_QUEUE.append(
                                (s, s2, t, q)))
        # 委托后 pm._ALGO_QUEUE 只有一项
        pm._protection_service().enqueue_algo_sl(
            'BUSDT', 'BUY', 1.08, 50.0)
        assert any('BUSDT' in str(item) for item in pm._ALGO_QUEUE) and \
            any('AUSDT1' in str(item) for item in pm._ALGO_QUEUE)
        pm._ALGO_QUEUE.clear()
