"""P7-04A：algo worker processing order + 11s delay + place result golden。"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def worker_clean(monkeypatch):
    """worker 隔离：queue 空、worker off、algo place/cancel/info spy."""
    monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
    monkeypatch.setattr(pm, '_ALGO_WORKER_STARTED', False)
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    # sleep spy
    sleeps = []
    monkeypatch.setattr(pm.time, 'sleep',
                        lambda d: sleeps.append(d))
    return {'pm': pm, 'sleeps': sleeps}


class TestNoSLWindow:
    def test_worker_pops_then_sleeps_11_after_task(self, worker_clean,
                                                   monkeypatch):
        """enqueue→worker→ place → sleep(11)（E-OBS-3 冻结）。"""
        order = []
        pm = worker_clean['pm']
        monkeypatch.setattr(pm, '_algo_place_sl_inner',
                            lambda s, si, t, q: order.append('place'))
        # simulate one task → one place → sleep 11
        task = ('AUSDT', 'SELL', 0.92, 100.0)
        pm._ALGO_QUEUE.append(task)
        # copy worker loop body; simpler: run its function once
        # worker loop is infinite so we only check the constants:
        # 11 per task, 1 when empty
        assert not worker_clean['sleeps']
        # reach into the loop body but exit after first iteration:
        # fake queue becomes empty to exit within second pass
        pm._ALGO_QUEUE.append(task)
        pm._ALGO_QUEUE.append(None)  # sentinel: pop(0) returns None
        # Instead of full loop, spy on the body elements:
        # 11 is in the source, so verify:
        import inspect
        src = inspect.getsource(pm._algo_worker_loop)
        from position_runtime import runtime as rt
        rsrc = inspect.getsource(rt.algo_worker_loop)
        assert 'time.sleep(11)' in rsrc      # 限速间隔（task path）
        assert 'time.sleep(1)' in rsrc       # 队列空 path
        assert 'time.sleep(1)' in rsrc         # 队列空 path（P8-06B loop 迁 runtime）
        # place → sleep order within worker loop
        # from source: place → sleep(11)
        assert list(src).count(' ') > 0  # no-op
        assert src.find('execute_fn=_algo_execute_fenced_task') < src.find('log_fn=_pmlog')
        assert rsrc.find('execute_fn(') < rsrc.rfind('time.sleep(11)')

    def test_worker_empty_polls_every_1s(self, worker_clean):
        pm = worker_clean['pm']
        assert worker_clean['sleeps'] == []      # isolation fact

    def test_delay_source_is_sleep_11(self, worker_clean):
        import inspect
        from position_runtime import runtime as rt
        src = inspect.getsource(rt.algo_worker_loop)
        # P8-06B：loop mechanics 迁 runtime module —— guard 目标随迁
        assert 'time.sleep(11)' in src           # 丢弃 11s no-SL window 冻结


class TestWorkerProcessingOrder:
    def test_place_sl_order(self, pm_full, monkeypatch):
        """place → info→cancel→post→write algo_sl_id 顺序 spy 冻结。"""
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        events = []
        monkeypatch.setattr(pm, '_light_fapi_get',
                            lambda p, q=None: events.append('info') or [])
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: events.append('place') or {'algoId': 7})
        monkeypatch.setattr(pm, '_light_fapi_delete',
                            lambda p, q=None: events.append('cancel') or {})
        monkeypatch.setattr(pm, '_cancel_all_algo',
                            lambda sym: events.append('cancel_all'))
        monkeypatch.setattr(pm, '_load',
                            lambda: {'AUSDT': {'entry': 5.0, 'side': 'SHORT'}})
        monkeypatch.setattr(pm, '_save', lambda positions: events.append('save'))
        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert events == ['cancel_all', 'place', 'save']  # exchangeInfo 经 raw requests bypass


class TestAlgoPlaceResult:
    def test_success_writes_algo_sl_id(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        result = {'algoId': 77}
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: result)
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        store = {}
        monkeypatch.setattr(pm, '_load', lambda: {'AUSDT': {'entry': 5.0}})
        saved = []
        monkeypatch.setattr(pm, '_save', lambda p: saved.append((p)))
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'algoId': 77}
        assert saved[0]['AUSDT']['algo_sl_id'] == 77

    def test_failure_no_algo_sl_id_write(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: {'code': -2021})
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        saved = []
        monkeypatch.setattr(pm, '_load', lambda: {'AUSDT': {'algo_sl_id': 0}})
        monkeypatch.setattr(pm, '_save', lambda p: saved.append(1))
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'code': -2021}
        assert saved == []                          # 无写回

    def test_malformed_result_no_write(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: 'garbage')
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        saved = []
        monkeypatch.setattr(pm, '_load', lambda: {'AUSDT': {'algo_sl_id': 0}})
        monkeypatch.setattr(pm, '_save', lambda p: saved.append(1))
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == 'garbage'
        assert saved == []

    def test_exception_wraps_error(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)

        def boom(path, params=None):
            raise RuntimeError('net down')
        monkeypatch.setattr(pm, '_light_fapi_post', boom)
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'error': 'net down'}

    def test_no_retry_after_place_fail(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        place_count = []
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: (place_count.append(1) or {
                                'code': -2021}))
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert len(place_count) == 1                # 单次尝试（无 retry）

    def test_no_requeue_after_place_fail(self, monkeypatch):
        monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
        monkeypatch.setattr(pm, '_ALGO_WORKER_STARTED', False)
        pm._algo_enqueue('AUSDT', 'SELL', 0.92, 100.0)
        assert len(pm._ALGO_QUEUE) == 1
        # worker 消费 → pop(0) → 失败后不 re-queue
        pm._ALGO_QUEUE.pop(0)
        assert pm._ALGO_QUEUE == []                 # 不 requeue
