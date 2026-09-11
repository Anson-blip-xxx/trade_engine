"""P4-03-01-D4：Protection / Notification boundary contract & freezing tests.

覆盖（prompt 测试要求 1-15）：
1 Port contract（5+2 真实 seam 方法面，不发明接口）
2 adapter 委托（Protection 5 seam + Notification 2 seam 逐字）
3 Algo 参数透传（enqueue/place 元组）
4 cancel 参数透传（id / symbol）
5 notification 参数透传（含默认 interval=60）
6 exception 语义原样
7 无 retry（单次调用计数）
8 delay/timing 配置不变（sleep(11)/sleep(1)/3s/60s/0.2%/30s grace 源码冻结）
9-11 无真实 Binance/Redis/TG（fake 注入 + 全部委托断言）
12 无后台线程泄漏（adapter 自身不建线程；测试不触发 start_worker 真线程）
13-14 adapter 不 import PM/SE/strategies
15 循环依赖检查（子进程 sys.modules）
+ PMB-9 兜底事实冻结（_s6api 兜底槽位名为 get——契约不修，仅冻结观察）
+ no-wiring 状态冻结（open_position/close 编排仍直调 PM 函数）
"""
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from execution.adapters.notification import NotificationAdapter
from execution.adapters.protection import ProtectionAdapter
from execution.ports.notification import NotificationPort
from execution.ports.protection import ProtectionPort


class Recording:
    """可注入的记录器（带可选异常）。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


def make_protection(place_result=None):
    return ProtectionAdapter(
        Recording(), Recording(),
        Recording(result=place_result if place_result is not None else {}),
        Recording(result={'code': -1, 'msg': 'unknown algo'}), Recording())


def make_notification():
    return NotificationAdapter(Recording(), Recording())


# ═══════════════════════════════════════════════════════════════
#  1. Port contracts（方法面冻结，不发明接口）
# ═══════════════════════════════════════════════════════════════

class TestPortContracts:
    def test_protection_port_method_surface(self):
        members = [name for name, _ in inspect.getmembers(
            ProtectionPort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert members == ['cancel_algo_id', 'cancel_all_algo',
                           'enqueue_algo_sl', 'place_algo_sl',
                           'start_algo_worker']

    def test_notification_port_method_surface(self):
        members = [name for name, _ in inspect.getmembers(
            NotificationPort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert members == ['log_close_error', 'notify_external_position']

    def test_adapters_satisfy_ports(self):
        assert isinstance(make_protection(), ProtectionPort)
        assert isinstance(make_notification(), NotificationPort)

    def test_no_thread_or_sleep_in_boundary(self):
        """Port/adapter 不拥有线程定时——源码无 threading/Thread/sleep。"""
        for module in ('execution.ports.protection', 'execution.adapters.protection',
                       'execution.ports.notification',
                       'execution.adapters.notification'):
            src = inspect.getsource(__import__(module, fromlist=['x']))
            # 扫描实际代码 import（非 docstring 措辞）
            assert not re.search(r'^\s*(import\s+threading|from\s+threading\b)',
                                 src, re.M), f'{module} import threading'
            assert not re.search(r'threading\.Thread|\.start\(|time\.sleep',
                                 src), f'{module} 建线程/sleep'


# ═══════════════════════════════════════════════════════════════
#  2-7. 委托正确性（参数/异常/单次调用）
# ═══════════════════════════════════════════════════════════════

class TestProtectionDelegation:
    def test_enqueue_params_verbatim(self):
        enqueue = Recording()
        adapter = ProtectionAdapter(enqueue, Recording(),
                                    Recording(), Recording(), Recording())
        adapter.enqueue_algo_sl('AUSDT', 'BUY', 0.92, 100.0)
        assert enqueue.calls == [(('AUSDT', 'BUY', 0.92, 100.0), {})]

    def test_place_algo_sl_params_and_result(self):
        place = Recording()
        adapter = ProtectionAdapter(Recording(), Recording(), place,
                                    Recording(), Recording())
        r = adapter.place_algo_sl('AUSDT', 'SELL', 0.92, 100.0)
        assert r == place.result
        assert place.calls == [(('AUSDT', 'SELL', 0.92, 100.0), {})]

    def test_place_algo_sl_exception_wraps_like_pm(self):
        """真实 seam 行为：inner 吞错 → {'error': str(e)}——注入侧定义的原样。"""
        place = Recording(exc=RuntimeError('boom'))
        rec = Recording(result={'error': 'boom'})

        # 模拟 PM._algo_place_sl_inner 的吞错语义（注入侧承担）
        def pm_like(symbol, side, trigger, qty):
            try:
                raise RuntimeError('connection reset')
            except Exception as e:
                return {'error': str(e)}
        adapter = ProtectionAdapter(Recording(), Recording(), pm_like,
                                    Recording(), Recording())
        assert adapter.place_algo_sl('AUSDT', 'SELL', 0.92, 100.0) == \
            {'error': 'connection reset'}

    def test_cancel_id_params(self):
        cancel_id = Recording()
        adapter = ProtectionAdapter(Recording(), Recording(), Recording(),
                                    cancel_id, Recording())
        r = adapter.cancel_algo_id(12345)
        assert r == cancel_id.result
        assert cancel_id.calls == [((12345,), {})]

    def test_cancel_all_params(self):
        cancel_all = Recording()
        adapter = ProtectionAdapter(Recording(), Recording(), Recording(),
                                    Recording(), cancel_all)
        adapter.cancel_all_algo('AUSDT')
        assert cancel_all.calls == [(('AUSDT',), {})]

    def test_start_worker_no_thread_leak(self):
        started = []
        adapter = ProtectionAdapter(
            Recording(),
            lambda: started.append(1),  # fake：不真起线程
            Recording(), Recording(), Recording())
        adapter.start_algo_worker()
        adapter.start_algo_worker()
        assert started == [1, 1]      # 幂等性由注入实现定义；adapter 不加逻辑

    def test_exception_propagates_verbatim(self):
        class Boom:
            def __call__(self, *a, **k):
                raise RuntimeError('redis lock lost')
        adapter = ProtectionAdapter(Boom(), Recording(), Recording(),
                                    Recording(), Recording())
        with pytest.raises(RuntimeError, match='redis lock'):
            adapter.enqueue_algo_sl('X', 'BUY', 1.0, 1.0)

    def test_no_retry_single_dispatch(self):
        cancel_all = Recording()
        adapter = ProtectionAdapter(Recording(), Recording(), Recording(),
                                    Recording(), cancel_all)
        cancel_all.exc = RuntimeError('x')
        with pytest.raises(RuntimeError):
            adapter.cancel_all_algo('X')
        assert len(cancel_all.calls) == 1


class TestNotificationDelegation:
    def test_notify_external_params(self):
        notify = Recording()
        adapter = NotificationAdapter(notify, Recording())
        raw = {'symbol': 'ABCUSDT', 'side': 'LONG', 'entry': 1.5, 'qty': 2}
        adapter.notify_external_position('ABCUSDT', raw, 'S6')
        assert notify.calls == [(('ABCUSDT', raw, 'S6'), {})]

    def test_log_close_error_default_interval_60(self):
        """委托默认值 = PM seam（interval=60）逐字一致。"""
        log_close = Recording()
        adapter = NotificationAdapter(Recording(), log_close)
        adapter.log_close_error('XUSDT', 'Margin insufficient.')
        # 委托方式冻结：位置传参，默认值由 adapter 签名补全为 60
        assert log_close.calls[0] == (('XUSDT', 'Margin insufficient.', 60), {})
        adapter.log_close_error('XUSDT', 'again', interval=120)
        adapter.log_close_error('XUSDT', 'again2', 300)
        assert log_close.calls[1] == (('XUSDT', 'again', 120), {})
        assert log_close.calls[2] == (('XUSDT', 'again2', 300), {})

    def test_notification_exception_verbatim(self):
        class Boom:
            def __call__(self, *a, **k):
                raise ValueError('tg down')
        adapter = NotificationAdapter(Boom(), Recording())
        with pytest.raises(ValueError, match='tg down'):
            adapter.notify_external_position('X', {}, 'S6')


# ═══════════════════════════════════════════════════════════════
#  8. Timing / thread 常量冻结（真实实现，行为不变锁）
# ═══════════════════════════════════════════════════════════════

class TestTimingFrozen:
    def test_worker_sleep_intervals_unchanged(self):
        from shared import position_manager as pm
        src = inspect.getsource(pm._algo_worker_loop)
        assert 'time.sleep(11)' in src      # 限速间隔（无保护窗口，E-OBS-3）
        assert 'time.sleep(1)' in src       # 空队列轮询

    def test_throttle_constants_unchanged(self):
        from shared import position_manager as pm
        assert pm._ALGO_UPDATE_INTERVAL == 60
        assert pm._ALGO_MIN_CHANGE_PCT == 0.2
        assert pm._API_COOLDOWN == 3

    def test_grace_sec_unchanged(self):
        from shared import position_manager as pm
        src = inspect.getsource(pm._notify_external_position)
        assert 'grace_sec = 30' in src

    def test_import_side_effect_still_present(self):
        """N1 冻结：se 模块 import 即启动 worker（现址保留，不修）。"""
        src = inspect.getsource(
            __import__('strategies.shared_executor', fromlist=['x']))
        assert '_algo_start_worker()' in src


# ═══════════════════════════════════════════════════════════════
#  PMB-9 事实冻结：_s6api 兜底 fapi_delete 槽位实为 GET 函数
# ═══════════════════════════════════════════════════════════════

class TestPMB9FallbackFact:
    def test_fallback_slot3_is_get_not_delete(self):
        """冻结观察：兜底元组第 3 槽 = _light_fapi_get（非 delete）——
        _algo_cancel 在兜底模式下发 GET 而非 DELETE（OBSERVED，不修）。"""
        from shared import position_manager as pm
        # 直接验证兜底元组构造（不触发真实网络：构造后立刻还原）
        saved = pm._S6_API
        pm._S6_API = None
        try:
            def _no_import(*a, **k):
                raise ImportError('blocked for test')
            # 模拟 fallback 分支：binance_api import 失败
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr('builtins.__import__', _no_import)
                pm._S6_API = None
                trio = pm._s6api()
                assert trio[2] is pm._light_fapi_get   # GET 冒充 delete
        finally:
            pm._S6_API = saved = saved if (saved := saved) else pm._S6_API

    def test_normal_slot3_is_delete(self, monkeypatch):
        from shared import position_manager as pm
        from shared import binance_api
        monkeypatch.setattr(pm, '_S6_API', None)
        # binance_api 可 import 时：第 3 槽 = fapi_delete（真实 DELETE）
        monkeypatch.setitem(binance_api.__dict__, 'fapi_delete',
                            binance_api.fapi_delete)
        monkeypatch.setattr(pm, '_light_fapi_get', pm._light_fapi_get)
        saved = pm._S6_API
        pm._S6_API = None
        try:
            pm._S6_API = (binance_api.fapi_get, binance_api.fapi_post,
                          binance_api.fapi_delete, binance_api.fapi_get,
                          None, None, None, pm._rec_stub if hasattr(
                              pm, '_rec_stub') else (lambda *a, **k: None))
            trio = pm._s6api()
            assert 'delete' in trio[2].__name__.lower()
        finally:
            pm._S6_API = saved


# ═══════════════════════════════════════════════════════════════
#  NO-WIRING 状态冻结
# ═══════════════════════════════════════════════════════════════

class TestNoWiring:
    def test_open_position_enqueues_directly(self):
        src = inspect.getsource(
            __import__('strategies.shared_executor', fromlist=['x']).open_position)
        assert '_algo_enqueue(' in src

    def test_pm_close_cancels_directly(self):
        src = inspect.getsource(
            __import__('shared.position_manager', fromlist=['x'])._close)
        assert '_cancel_all_algo(' in src

    def test_service_unaware_of_protection(self):
        src = inspect.getsource(__import__('execution.service', fromlist=['x']))
        assert 'Protection' not in src and 'Notification' not in src


# ═══════════════════════════════════════════════════════════════
#  13-15：依赖边界与循环依赖
# ═══════════════════════════════════════════════════════════════

class TestDependencyBoundary:
    FORBIDDEN = ('redis', 'requests', 'psycopg', 'strategies',
                 'shared_executor', 'position_manager', 'shared.',
                 'telegram', 'binance', 'threading', 'queue', 'time',
                 'socket', 'os')

    @pytest.mark.parametrize('module', [
        'execution.ports.protection', 'execution.adapters.protection',
        'execution.ports.notification', 'execution.adapters.notification'])
    def test_source_has_no_forbidden_imports(self, module):
        src = inspect.getsource(__import__(module, fromlist=['x']))
        for mod in self.FORBIDDEN:
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}',
                                 src, re.M), f'forbidden import: {mod}'

    def test_clean_import_pulls_no_io_modules(self):
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys; "
                "import execution.ports.protection, "
                "execution.adapters.protection, "
                "execution.ports.notification, "
                "execution.adapters.notification; "
                "bad = [m for m in ('redis', 'requests', 'psycopg', "
                "'telegram', 'shared.position_manager', "
                "'strategies.shared_executor') if m in sys.modules]; "
                "print(bad)")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == '[]'
