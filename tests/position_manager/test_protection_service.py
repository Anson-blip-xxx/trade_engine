"""P7-04B：ProtectionService contract tests（service 面单独覆盖）。"""
import pytest
from position_protection.service import ProtectionService


@pytest.fixture
def svc_env():
    class Fakeregistry:
        def __init__(self):
            self.store = {
                'enqueued': [],
                'starts': 0,
                'placed': [],
                'canceled_ids': [],
                'cancel_all': [],
                'result': {'algoId': 1},
            }

        def enqueue_fn(self, s, side, t, q):
            self.store['enqueued'].append((s, side, t, q))

        def start_worker_fn(self):
            self.store['starts'] += 1

        def place_fn(self, s, side, t, q):
            self.store['placed'].append((s, side, t, q))
            return self.store['result']

        def cancel_id_fn(self, aid):
            self.store['canceled_ids'].append(aid)
            return {'status': 'ok'}

        def cancel_all_fn(self, s):
            self.store['cancel_all'].append(s)

        def store_snapshot(self):
            return self.store

        def svc(self):
            return ProtectionService(
                enqueue_fn=self.enqueue_fn,
                start_worker_fn=self.start_worker_fn,
                place_algo_sl_fn=self.place_fn,
                cancel_id_fn=self.cancel_id_fn,
                cancel_all_fn=self.cancel_all_fn)
    return Fakeregistry()


class TestServiceContract:
    def test_service_creation_minimal(self, svc_env):
        svc = svc_env.svc()
        assert svc is not None

    def test_api_surface_matches_protection_port(self, svc_env):
        import inspect
        from execution.ports.protection import ProtectionPort
        svc_class = ProtectionService
        svc_methods = sorted(name for name, _ in inspect.getmembers(
            svc_class, predicate=inspect.isfunction)
            if not name.startswith('_'))
        port_methods = sorted(name for name, _ in inspect.getmembers(
            ProtectionPort, predicate=inspect.isfunction)
            if not name.startswith('_'))
        assert svc_methods == port_methods

    def test_enqueue_passthrough(self, svc_env):
        svc = svc_env.svc()
        state = svc_env.store_snapshot()
        svc.enqueue_algo_sl('AUSDT', 'SELL', 0.92, 100.0)
        assert state['enqueued'] == [('AUSDT', 'SELL', 0.92, 100.0)]

    def test_start_worker_passthrough(self, svc_env):
        svc = svc_env.svc()
        state = svc_env.store_snapshot()
        svc.start_algo_worker()
        svc.start_algo_worker()
        assert state['starts'] == 2               # passthrough（caller gate）

    def test_place_algo_sl_passthrough(self, svc_env):
        svc = svc_env.svc()
        state = svc_env.store_snapshot()
        r = svc.place_algo_sl('AUSDT', 'SELL', 0.92, 100.0)
        assert r['algoId'] == 1
        assert state['placed'] == [('AUSDT', 'SELL', 0.92, 100.0)]

    def test_cancel_id_passthrough(self, svc_env):
        svc = svc_env.svc()
        state = svc_env.store_snapshot()
        r = svc.cancel_algo_id(12345)
        assert r == {'status': 'ok'}
        assert state['canceled_ids'] == [12345]

    def test_cancel_all_passthrough(self, svc_env):
        svc = svc_env.svc()
        state = svc_env.store_snapshot()
        svc.cancel_all_algo('AUSDT')
        assert state['cancel_all'] == ['AUSDT']
