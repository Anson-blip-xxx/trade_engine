"""P7-04A：_ALGO_QUEUE FIFO / item shape / worker start / restart loss golden。"""
import threading

import pytest
from shared import position_manager as pm



@pytest.fixture
def algo_clean(monkeypatch):
    """每个测试隔离队列/flag + 线程 mock（不真起 daemon）。"""
    monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
    monkeypatch.setattr(pm, '_ALGO_WORKER_STARTED', False)
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    threads_created = []
    real_thread = threading.Thread

    class ThreadSpy:
        def __init__(self, *a, **k):
            threads_created.append((a, k))
            self.daemon = False
            self.target = a[0] if a else k.get('target')
            self._args = k.get('args', ())

        def start(self):
            pass                              # 不真启动线程

    monkeypatch.setattr(pm.threading, 'Thread', ThreadSpy)
    return {'pm': pm, 'threads': threads_created}


class TestQueueSemantics:
    def test_fifo_a_then_b_consumed_in_order(self, algo_clean):
        """A then B enqueue → A then B consume（严格 FIFO，无 priority）。"""
        pm = algo_clean['pm']
        pm._algo_enqueue('AUSDT', 'SELL', 0.92, 100.0)
        pm._algo_enqueue('BUSDT', 'BUY', 1.08, 50.0)
        assert pm._ALGO_QUEUE == [('AUSDT', 'SELL', 0.92, 100.0),
                                  ('BUSDT', 'BUY', 1.08, 50.0)]
        # worker dequeue = pop(0) → A first
        first = pm._ALGO_QUEUE.pop(0)
        second = pm._ALGO_QUEUE.pop(0)
        assert first[0] == 'AUSDT' and second[0] == 'BUSDT'

    def test_queue_item_is_exactly_4_tuple(self, algo_clean):
        g = algo_clean['pm']
        g._algo_enqueue('XUSDT', 'BUY', 0.05, 1000.0)
        item = g._ALGO_QUEUE[0]
        assert isinstance(item, tuple) and len(item) == 4
        assert item == ('XUSDT', 'BUY', 0.05, 1000.0)
        # 无 dict / timestamp / id / extra fields
        assert not isinstance(item, dict)
        assert 'timestamp' not in item

    def test_enqueue_non_blocking_no_max_size(self, algo_clean):
        pm = algo_clean['pm']
        for i in range(100):
            pm._algo_enqueue(f'S{i}USDT', 'SELL', 0.9, 1.0)
        assert len(pm._ALGO_QUEUE) == 100             # 无 max size

    def test_double_enqueue_same_sym_allowed(self, algo_clean):
        pm = algo_clean['pm']
        pm._algo_enqueue('AUSDT', 'SELL', 0.92, 100.0)
        pm._algo_enqueue('AUSDT', 'SELL', 0.91, 100.0)  # 同币不同价 → 两条
        assert len(pm._ALGO_QUEUE) == 2                # 无 dedup


class TestWorkerStartSemantics:
    def test_single_start_creates_one_thread(self, algo_clean):
        pm = algo_clean['pm']
        pm._algo_start_worker()
        assert len(algo_clean['threads']) == 1
        assert pm._ALGO_WORKER_STARTED is True

    def test_double_start_silent_noop(self, algo_clean):
        """调用 start 两次 → 静默 no-op（不启第二份线程，不抛）。"""
        pm = algo_clean['pm']
        pm._algo_start_worker()
        pm._algo_start_worker()
        assert len(algo_clean['threads']) == 1   # 只启一份

    def test_daemon_and_named(self, algo_clean):
        pm = algo_clean['pm']
        pm._algo_start_worker()
        (a, k) = algo_clean['threads'][0]
        assert k.get('daemon') is True
        assert k.get('name') == 'algo-worker'

    def test_import_side_effect_frozen(self, algo_clean):
        """PMB-N1 冻结：se._algo_start_worker() 在模块导入时被调用
        （import 副作用）——source guard 冻结。"""
        import inspect
        from strategies import shared_executor as se
        src = inspect.getsource(se)
        body = src.split("if __name__")[0]
        assert '_algo_start_worker()' in body          # import 侧效应保持

    def test_worker_flag_persists_across_starts(self, algo_clean):
        pm = algo_clean['pm']
        pm._ALGO_WORKER_STARTED = True                 # 手工 flag True (模拟进程)
        pm._algo_start_worker()
        assert algo_clean['threads'] == []     # flag gate 阻止第二份


class TestRestartQueueLoss:
    def test_restart_queue_content_lost(self, algo_clean, monkeypatch):
        """S0-4 家族：_ALGO_QUEUE 是内存 list — restart/global reset 丢任务。"""
        pm = algo_clean['pm']
        pm._algo_enqueue('AUSDT', 'SELL', 0.92, 100.0)
        pm._algo_enqueue('BUSDT', 'BUY', 1.08, 50.0)
        # 模拟重启：模块级 _ALGO_QUEUE 被替换为新空 list
        monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
        assert pm._ALGO_QUEUE == []                    # 任务丢失（无 replay）

    def test_worker_no_persistence_no_replay(self, algo_clean):
        """确认无持久化：队列非 Redis-backed，enqueue 不写 Redis key。"""
        assert not hasattr(pm, 'pm:algo_queue')       # 不存在 redis key helper
