"""P5-03：S3 state boundary contract + lifecycle parity（injected vs legacy）。

Store = dict-protocol 精确镜像（get 引用/`[]=`/pop/items 迭代序）；
生产 default = legacy `_event_states`/`_fb_state`（普通 dict 同协议）。
"""
import pytest

from s3.state import BreakoutStateStore, EventStateStore


NOW = 1_700_000_000.0


def pulse(strength=50, sym='AUSDT'):
    return {'type': 'PULSE_UP', 'symbol': sym, 'strength': strength}


def snap(store):
    return {k: dict(v) for k, v in store.items()}


def kc(t, h, l, c):
    return {'t': t, 'o': c, 'h': h, 'l': l, 'c': c, 'v': 0}


# ═══════════════════════════════════════════════════════════════
#  Store contract（dict 语义镜像 / aliasing / 独立性 / 顺序）
# ═══════════════════════════════════════════════════════════════

class TestStoreContract:
    def test_get_reference_not_copy(self):
        store = EventStateStore()
        rec = {'state': 'ACTIVE', 'strength': 50}
        store['K'] = rec
        got = store.get('K')
        assert got is rec
        got['state'] = 'END'
        assert store.get('K')['state'] == 'END'

    def test_items_insertion_order(self):
        store = EventStateStore()
        for k in ('C_B', 'A_A', 'B_C'):
            store[k] = {'state': 'ACTIVE'}
        assert [k for k, _ in store.items()] == ['C_B', 'A_A', 'B_C']

    def test_pop_semantics(self):
        store = EventStateStore()
        store['K'] = {'state': 'ACTIVE'}
        assert store.pop('K') == {'state': 'ACTIVE'}
        assert store.pop('K') is None
        assert store.pop('NEVER') is None          # 无异常

    def test_two_stores_independent(self):
        a, b = EventStateStore(), EventStateStore()
        a['AUSDT_PULSE_UP'] = {'state': 'ACTIVE', 'strength': 1}
        assert b.get('AUSDT_PULSE_UP') is None
        assert len(b) == 0

    def test_backing_dict_aliasing(self):
        backing = {}
        store = EventStateStore(backing)
        store['K'] = {'state': 'UPDATE'}
        assert backing == {'K': {'state': 'UPDATE'}}
        backing['ADD'] = {'state': 'ACTIVE'}
        assert store.get('ADD') is not None

    def test_breakout_default_returns_ref(self):
        store = BreakoutStateStore()
        default = {'state': 'IDLE'}
        got = store.get('X', default)
        assert got is default
        got['state'] = 'BREAKING_HIGH'
        assert default['state'] == 'BREAKING_HIGH'


# ═══════════════════════════════════════════════════════════════
#  Lifecycle parity：legacy vs 注入 store（同一序列逐段对拍）
# ═══════════════════════════════════════════════════════════════

class TestLifecycleParity:
    @pytest.fixture(autouse=True)
    def _setup(self, s3m):
        self.s3 = s3m
        self.inj = EventStateStore()

    def test_series_identical(self):
        seq = [(NOW, 50), (NOW + 10, 55), (NOW + 25, 70),  # cool 跳过，delta 放行
               (NOW + 40, 70), (NOW + 500, 80)]
        legacy_msgs, legacy_hist = [], []
        inj_msgs, inj_hist = [], []
        for now, strength in seq:
            m = self.s3._update_event_state(pulse(strength), now)
            legacy_msgs.append(None if m is None else dict(m))
            legacy_hist.append(snap(self.s3._event_states))
            m2 = self.s3._update_event_state(pulse(strength), now,
                                             state_store=self.inj)
            inj_msgs.append(None if m2 is None else dict(m2))
            inj_hist.append(snap(self.inj))
        assert legacy_msgs == inj_msgs
        assert legacy_hist == inj_hist

    def test_end_ordering_insertion_order(self):
        """多键过期 → END 输出按底层 dict 插入序（同参对拍 legacy vs 注入）。"""
        for sym, now in (('A', NOW), ('B', NOW + 1), ('C', NOW + 2)):
            self.s3._update_event_state(
                {'type': 'PULSE_UP', 'symbol': sym, 'strength': 50}, now)
        ended_legacy = self.s3._end_expired_events(['A', 'B', 'C'], NOW + 500)
        # 注入侧重放同一序列（fresh store）
        for sym, now in (('A', NOW), ('B', NOW + 1), ('C', NOW + 2)):
            self.s3._update_event_state(
                {'type': 'PULSE_UP', 'symbol': sym, 'strength': 50}, now,
                state_store=self.inj)
        ended_inj = self.s3._end_expired_events(['A', 'B', 'C'], NOW + 500,
                                                state_store=self.inj)
        assert ended_legacy == ended_inj
        assert [e['symbol'] for e in ended_inj] == ['A', 'B', 'C']

    def test_end_injected_aliasing_and_removal(self):
        self.s3._update_event_state(pulse(), NOW, state_store=self.inj)
        rec_ref = self.inj.get('AUSDT_PULSE_UP')
        ended = self.s3._end_expired_events(['AUSDT'], NOW + 301,
                                            state_store=self.inj)
        assert rec_ref['state'] == 'END'      # 原地突变（aliasing 保持）
        assert self.inj.get('AUSDT_PULSE_UP') is None
        assert ended[0]['duration'] >= 300

    def test_time_boundaries_identical_via_injection(self):
        """29s skip / 30s allow / delta 19 skip / 20 allow —— 注入路径同值。"""
        self.s3._update_event_state(pulse(50), NOW, state_store=self.inj)
        assert self.s3._update_event_state(
            pulse(60), NOW + 29, state_store=self.inj) is None
        out30 = self.s3._update_event_state(pulse(60), NOW + 30,
                                            state_store=self.inj)
        assert out30 is not None and out30['state'] == 'UPDATE'
        self.s3._update_event_state(pulse(61), NOW + 35, state_store=self.inj)
        assert self.s3._update_event_state(
            pulse(79), NOW + 40, state_store=self.inj) is None      # delta18<20 且 5s<30
        out20 = self.s3._update_event_state(pulse(81), NOW + 45,
                                            state_store=self.inj)
        assert out20 is not None                              # delta21 ≥20 放行


class TestMultiInstanceParity:
    def test_two_injected_stores_duplicate_events(self, s3m):
        """S3-4 保持：两 store 同输入 → 双发 ACTIVE（无 distributed dedup）。"""
        a, b = EventStateStore(), EventStateStore()
        m1 = s3m._update_event_state(pulse(), NOW, state_store=a)
        m2 = s3m._update_event_state(pulse(), NOW, state_store=b)
        assert m1['state'] == m2['state'] == 'ACTIVE'
        assert m1['since'] == m2['since'] == NOW


# ═══════════════════════════════════════════════════════════════
#  FAILED_BREAKOUT 状态注入 parity + S3-3 冻结
# ═══════════════════════════════════════════════════════════════

class TestBreakoutInjectedParity:
    def test_transitions_identical(self, s3m, clock):
        """IDLE→BREAKING_HIGH→rejected→IDLE 在 legacy/injected 下结果一致。"""
        raw_4h = [kc(10, 105.0, 95.0, 100.0), kc(9, 100.0, 95.0, 100.0)]
        raw_15m = [kc(20, 106.0, 105.0, 105.5), kc(19, 105.0, 104.0, 105.0),
                   kc(18, 104.0, 103.0, 104.0)]
        raw_4h2 = [kc(11, 105.0, 95.0, 100.0), kc(10, 105.0, 95.0, 100.0)]
        raw_15m2 = [kc(21, 104.9, 104.0, 104.8), kc(20, 106.0, 102.0, 103.0),
                    kc(20, 105.0, 104.0, 104.0)]
        traj = []
        for store in (None, BreakoutStateStore()):
            events = []
            s3m._detect_failed_breakout('AUSDT', raw_4h, raw_15m, events,
                                        state_store=store, time_fn=clock.time)
            s3m._detect_failed_breakout('AUSDT', raw_4h2, raw_15m2, events,
                                        state_store=store, time_fn=clock.time)
            traj.append(([dict(e) for e in events],
                         None if store is None else snap(store)))
        assert traj[0][0] == traj[1][0]
        ev = traj[0][0][0]
        assert ev['type'] == 'FAILED_BREAKOUT' and ev['direction'] == 'HIGH'
        # 状态一致性：injected store 与 legacy `_fb_state` 最终态一致
        assert snap(s3m._fb_state) == traj[1][1]
        assert traj[1][1]['AUSDT']['state'] == 'IDLE'

    def test_s33_unreachable_real_chain(self, s3m, redis_spy):
        """S3-3：注入态下真实 window 链（无 close_pos）→ 无 breakout_confirmed。"""
        n = 120
        klines = [{'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0,
                   'v': 10.0, 'tbv': 0.5} for i in range(n)]
        klines = sorted(klines, key=lambda x: -x['t'])
        s3m._symbol_klines['TESTUSDT'] = klines
        s3m.compute_and_detect(['TESTUSDT'])
        snapshot = redis_spy.store['event:s3']
        assert not any(e.get('breakout_confirmed') is True
                       for e in snapshot['events'])


# ═══════════════════════════════════════════════════════════════
#  架构纯度与注入面冻结
# ═══════════════════════════════════════════════════════════════

class TestStateArchitecture:
    def test_state_module_imports_stdlib_only(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / 's3' / 'state.py').read_text()
        tree = ast.parse(src)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split('.')[0])
        assert roots == {'__future__', 'typing'}

    def test_injection_points_frozen(self, s3m):
        import inspect
        assert 'state_store=None' in inspect.getsource(s3m._update_event_state)
        assert 'state_store=None' in inspect.getsource(s3m._end_expired_events)
        src3 = inspect.getsource(s3m._detect_failed_breakout)
        assert 'state_store=None' in src3 and 'time_fn=None' in src3

    def test_state_subprocess_clean_import(self):
        import subprocess
        import sys
        from pathlib import Path
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys, threading\n"
                "before = threading.active_count()\n"
                "import s3.state\n"
                "after = threading.active_count()\n"
                "bad = [m for m in ('redis', 'requests', 'psycopg',\n"
                "                   'websocket', 'shared.redis_store',\n"
                "                   'strategies.s3_orderflow')\n"
                "        if m in sys.modules]\n"
                "print(before, after, bad)\n")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        before, after, bad = out.stdout.strip().split(maxsplit=2)
        assert before == after and bad == '[]'
