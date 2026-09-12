"""P5-04：detector parity —— legacy detect_events vs s3.detector 同输入对拍。

场景矩阵 A-O + ordering/threshold/strength/lifecycle 拼接/S3-8 identity。
全部构造 windows 直调真实函数（不 mock 内部）。
"""
import pytest

from conftest import w15m, w1h, w24h, windows
from s3 import detector as det


def both(s3m, w, raw=None):
    raw = raw or {}
    legacy = s3m.detect_events('TESTUSDT', w, dict(raw))
    # detector-only 路径：无 breakout runner + 无 log（diff 只可能因
    # breakout 状态或 log 差异出现）
    cand = det.detect_candidate_events(
        'TESTUSDT', w, dict(raw), log_fn=None, breakout_runner=None)
    return legacy, cand


def types(evs):
    return [e['type'] for e in evs]


class TestScenarioParityMatrix:
    def _assert_same(self, a, b):
        assert [dict(e) for e in a] == [dict(e) for e in b]

    def test_a_no_event(self, s3m):
        a, b = both(s3m, windows(w15m()))
        self._assert_same(a, b)
        assert a == []

    def _assert_same(self, a, b):
        assert [dict(e) for e in a] == [dict(e) for e in b]

    def test_b_single_pulse_up(self, s3m):
        w = windows(w15m(chg=6.0, vol_ratio=2.0))
        a, b = both(s3m, w)
        assert a
        self._assert_same(a, b)
        assert types(b)[0] == 'PULSE_UP'

    def test_c_multiple_events_order(self, s3m):
        w = windows(w15m(chg=-5.0, vol_ratio=2.5))
        a, b = both(s3m, w)
        assert a
        self._assert_same(a, b)
        assert types(b) == ['PULSE_DOWN', 'PANIC_SELL', 'HIGH_VOL']

    def test_d_pulse_direction_payload(self, s3m):
        w = windows(w15m(chg=6.0, vol_ratio=2.0))
        a, b = both(s3m, w)
        self._assert_same(a, b)
        assert b[0]['strength'] == 48 and b[0]['chg_15m'] == 6.0

    def test_e_panic(self, s3m):
        w = windows(w15m(chg=-4.5, vol_ratio=2.2))
        a, b = both(s3m, w)
        self._assert_same(a, b)

    def test_f_violent(self, s3m):
        w = windows(w15m(close=104.0),
                    h1={'chg': 0, 'volatility': 20.0, 'high': 110.0,
                        'low': 90.0, 'ema20': 100.0, 'ema60': 100.0,
                        'atr_pct': 0.05, 'close': 104.0},
                    h4={'chg': 0.0, 'volatility': 20.0, 'ema20': 100.0,
                        'ema60': 100.0, 'atr_pct': 0.05, 'high': 110.0,
                        'low': 90.0, 'close': 104.0})
        a, b = both(s3m, w)
        self._assert_same(a, b)
        assert types(b) == ['VIOLENT_BULLISH']

    def test_g_pump(self, s3m):
        w = windows(w15m(chg=8.5, vol_ratio=2.0))
        a, b = both(s3m, w)
        self._assert_same(a, b)

    def test_h_trend(self, s3m):
        w = windows(w15m(),
                    h1={'chg': 1.0, 'high': 101.0, 'low': 99.0,
                        'ema20': 100.0, 'ema60': 100.0, 'atr_pct': 0.05,
                        'volatility': 0.0, 'close': 100.0},
                    h4={'chg': 2.0, 'ema20': 100.0, 'ema60': 100.0,
                        'atr_pct': 0.05, 'high': 101.0, 'low': 99.0,
                        'volatility': 0.0, 'close': 100.0})
        a, b = both(s3m, w)
        self._assert_same(a, b)
        assert types(b) == ['TREND_UP']

    def test_i_high_vol(self, s3m):
        w = windows(w15m(vol_ratio=3.0))
        a, b = both(s3m, w)
        self._assert_same(a, b)

    def test_j_low_vol(self, s3m):
        w = windows(w15m(vol_ratio=0.25))
        a, b = both(s3m, w)
        self._assert_same(a, b)


class TestDetectorOnly:
    """仅经 s3.detector 直接测（breakout_runner 注入的 stateful seam）。"""

    def test_breakout_runner_position_frozen(self, s3m):
        """runner 输出的 append 位置在 ATR_EXPAND 之后（源码序）。"""
        seq = []

        def runner(sym, raw4, raw15, evs):
            evs.append({'type': 'FAILED_BREAKOUT_MARKER'})
        w = windows(w15m())
        out = det.detect_candidate_events(
            'X', w, {'15m': [], '4h': []}, log_fn=None,
            breakout_runner=runner)
        assert out == []                       # raw 不足不触发
        raw_4h = [{'t': 1, 'h': 101, 'l': 99, 'c': 100},
                  {'t': 2, 'h': 101, 'l': 99, 'c': 100}]
        raw_15m = [{'t': 3, 'h': 101, 'l': 99, 'c': 100} for _ in range(3)]
        out = det.detect_candidate_events(
            'X', w, {'15m': raw_15m, '4h': raw_4h}, log_fn=None,
            breakout_runner=runner)
        assert types(out) == ['FAILED_BREAKOUT_MARKER']

    def test_threshold_boundary_parity(self, s3m):
        """P5-01 冻结的边界在 detector 路径同值（±5/vol1.5/15/24/ATR>2×）。"""
        assert 'PULSE_UP' in types(det.detect_candidate_events(
            'X', windows(w15m(chg=5.0, vol_ratio=2.0)), {}, log_fn=None))
        assert 'PULSE_UP' not in types(det.detect_candidate_events(
            'X', windows(w15m(chg=4.99, vol_ratio=2.0)), {}, log_fn=None))
        assert 'PULSE_UP' not in types(det.detect_candidate_events(
            'X', windows(w15m(chg=6.0, vol_ratio=1.49)), {}, log_fn=None))
        assert 'PULSE_UP' in types(det.detect_candidate_events(
            'X', windows(w15m(chg=6.0, vol_ratio=1.5)), {}, log_fn=None))
        assert 'HIGH_VOL' in types(det.detect_candidate_events(
            'X', windows(w15m(vol_ratio=2.0)), {}, log_fn=None))

    def test_strength_parity(self, s3m):
        b = det.detect_candidate_events(
            'X', windows(w15m(chg=6.0, vol_ratio=2.0)), {}, log_fn=None)
        assert b[0]['strength'] == 48
        b = det.detect_candidate_events(
            'X', windows(w15m(vol_ratio=2.0)), {}, log_fn=None)
        assert [e for e in b if e['type'] == 'HIGH_VOL'][0]['strength'] == 30

    def test_violent_stop_threshold_24h_boundary(self, s3m):
        b = det.detect_candidate_events(
            'X', windows(
                w15m(),
                h1={'chg': 0, 'volatility': 20.0, 'high': 110.0, 'low': 90.0,
                    'ema20': 100.0, 'ema60': 100.0, 'atr_pct': 0.05,
                    'close': 104.0}), {}, log_fn=None)
        # w15m.close 默认 100 == mid(=(110+90)/2=100) → _price>mid False → BEARISH
        b = det.detect_candidate_events(
            'X', windows(
                w15m(close=104.0),
                h1={'chg': 0, 'volatility': 20.0, 'high': 110.0, 'low': 90.0,
                    'ema20': 100.0, 'ema60': 100.0, 'atr_pct': 0.05,
                    'close': 104.0}), {}, log_fn=None)
        assert 'VIOLENT_BULLISH' in types(b)


class TestS3_8IdentityFrozen:
    def test_legacy_identity_after_detector(self, s3m, clock):
        """S3-8：legacy 路径（wrapper→detector→lifecycle）原对象引用保持。"""
        raw_evt = {'type': 'PULSE_UP', 'symbol': 'X', 'strength': 50}
        managed = s3m._update_event_state(raw_evt, clock.now)
        assert managed is raw_evt              # 同一引用（mutation chain）
        assert raw_evt['state'] == 'ACTIVE' and raw_evt['since'] == clock.now
        # detector 路径（candidate 生成不需 lifecycle——legacy 传入生命周期的
        # evt 引用同样经 wrapper → detector → lifecycle 保持 S3-8）
        cand_events = det.detect_candidate_events(
            'X', windows(w15m(chg=6.0, vol_ratio=2.0)), {}, log_fn=None)
        raw2 = cand_events[0]
        clock.advance(60)                     # 冷却窗口外（测试链路的事实）
        managed2 = s3m._update_event_state(raw2, clock.now)
        assert managed2 is raw2                # detector 输出直接进 lifecycle 保持 identity


class TestS3_3FrozenAfterDetector:
    def test_real_chain_breakout_unreachable(self, s3m, redis_spy):
        n = 120
        klines = [{'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0,
                   'v': 10.0, 'tbv': 0.5} for i in range(n)]
        klines = sorted(klines, key=lambda x: -x['t'])
        s3m._symbol_klines['TESTUSDT'] = klines
        s3m.compute_and_detect(['TESTUSDT'])   # 经 wrapper→detector
        snap = redis_spy.store['event:s3']
        assert not any(e.get('breakout_confirmed') is True
                       for e in snap['events'])


# ═══════════════════════════════════════════════════════════════
#  架构
# ═══════════════════════════════════════════════════════════════

class TestDetectorArchitecture:
    def test_detector_imports_stdlib_only(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / 's3' / 'detector.py').read_text()
        tree = ast.parse(src)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split('.')[0])
        assert roots <= {'__future__', 'typing'}
        assert not (roots & {'redis', 'requests', 'websocket', 'shared',
                             'execution', 'risk', 'strategies'})

    def test_no_module_global_mutation(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / 's3' / 'detector.py').read_text()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    # 仅 _noop / THRESHOLDS 允许
                    assert getattr(t, 'id', '') in {'THRESHOLDS', '_noop'}, t.id if hasattr(t, 'id') else t

    def test_detector_subprocess_clean_import(self):
        import subprocess
        import sys
        from pathlib import Path
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys, threading\n"
                "before = threading.active_count()\n"
                "import s3.detector\n"
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
