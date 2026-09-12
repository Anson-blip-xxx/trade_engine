"""P5-01：S3 生命周期 / 冷却 / failed-breakout 状态机 golden（fake clock，无 sleep）。"""
import pytest

from conftest import w15m, w1h, windows

NOW = 1_700_000_000.0


def pulse(strength=50, sym='AUSDT'):
    return {'type': 'PULSE_UP', 'symbol': sym, 'strength': strength}


class TestEventLifecycle:
    def test_first_seen_active(self, s3m, clock):
        out = s3m._update_event_state(pulse(), NOW)
        assert out['state'] == 'ACTIVE'
        st = s3m._event_states['AUSDT_PULSE_UP']
        assert st == {'state': 'ACTIVE', 'strength': 50, 'ts': NOW,
                      'sent_ts': NOW}
        assert out['since'] == NOW

    def test_within_cooldown_small_delta_skipped(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        clock.advance(29)                                 # <30s
        assert s3m._update_event_state(
            {'type': 'PULSE_UP', 'symbol': 'AUSDT', 'strength': 55}, clock.time()) \
            is None                                       # delta=5 < 20

    def test_at_cooldown_boundary_resends(self, s3m, clock):
        """真实代码条件：time<30 AND delta<20 → skip；30s 整点不再 skip。"""
        s3m._update_event_state(pulse(), NOW)
        clock.advance(30)                                 # 30s 边界
        out = s3m._update_event_state(
            {'type': 'PULSE_UP', 'symbol': 'AUSDT', 'strength': 52},
            clock.time())
        assert out is not None                             # 30 边界即放行
        assert out['state'] == 'UPDATE'

    def test_strength_delta_20_resends_within_cooldown(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        clock.advance(10)
        out = s3m._update_event_state(
            {'type': 'PULSE_UP', 'symbol': 'AUSDT', 'strength': 70},
            clock.time())                                  # delta 20 ≥ 20 → 放行
        assert out is not None and out['state'] == 'UPDATE'
        assert out['since'] == NOW                         # since = 原首发 ts

    def test_delta_19_still_skipped(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        clock.advance(10)
        assert s3m._update_event_state(
            {'type': 'PULSE_UP', 'symbol': 'AUSDT', 'strength': 69},
            clock.time()) is None

    def test_different_type_not_deduped(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        out = s3m._update_event_state(
            {'type': 'PUMP_UP', 'symbol': 'AUSDT', 'strength': 50}, NOW)
        assert out['state'] == 'ACTIVE'                    # (sym,type) 不同键

    def test_different_symbol_not_deduped(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        out = s3m._update_event_state(
            {'type': 'PULSE_UP', 'symbol': 'BUSDT', 'strength': 50}, NOW)
        assert out['state'] == 'ACTIVE'

    def test_after_end_state_becomes_active_again(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        s3m._event_states['AUSDT_PULSE_UP']['state'] = 'END'
        clock.advance(60)
        out = s3m._update_event_state(pulse(60), clock.time())
        assert out['state'] == 'ACTIVE'                    # 非 ACTIVE/UPDATE → ACTIVE


class TestEventEnd:
    def test_end_after_max_age_300(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        clock.advance(301)
        ended = s3m._end_expired_events(['AUSDT'], clock.time())
        assert ended == [{'type': 'PULSE_UP', 'symbol': 'AUSDT',
                          'strength': 50, 'state': 'END', 'since': NOW,
                          'duration': 301}]
        assert 'AUSDT_PULSE_UP' not in s3m._event_states    # 已删除

    def test_not_expired_within_300(self, s3m, clock):
        s3m._update_event_state(pulse(), NOW)
        clock.advance(299)
        assert s3m._end_expired_events(['AUSDT'], clock.time()) == []
        assert 'AUSDT_PULSE_UP' in s3m._event_states

    def test_end_respects_ts_column_not_sent(self, s3m, clock):
        """age 用 state['ts']（最后一次更新时刻），300 计时随每次续发重置。"""
        s3m._update_event_state(pulse(), NOW)
        for t in (10, 20, 30):
            clock.advance(30)
            s3m._update_event_state({'type': 'PULSE_UP', 'symbol': 'AUSDT',
                                     'strength': 70 + t}, clock.time())
        clock.advance(299)
        assert s3m._end_expired_events(['AUSDT'], clock.time()) == []

    def test_key_parsing_sym_with_underscore_ok(self, s3m, clock):
        """key split('_',1) 首段为 symbol；PERP 后缀不影响此处（以首拆为准）。"""
        s3m._update_event_state(
            {'type': 'PULSE_UP', 'symbol': '1000PEPE', 'strength': 50}, NOW)
        clock.advance(400)
        ended = s3m._end_expired_events(['1000PEPE', 'DUMMY_RUBBISH_KEY_'],
                                        clock.time())
        assert ended == [{'type': 'PULSE_UP', 'symbol': '1000PEPE',
                          'strength': 50, 'state': 'END', 'since': NOW,
                          'duration': 400}]


class TestFailedBreakoutStateMachine:
    def test_breakout_high_then_rejection_emits(self, s3m, clock):
        """真实调用：4h raw（最新在前）+ 15m raw；prev = 4h[1:]。"""
        def kc(t, h, l, c):
            return {'t': t, 'o': c, 'h': h, 'l': l, 'c': c, 'v': 0}

        raw_4h = [kc(10, 105.0, 95.0, 100.0),   # 最新 4h
                  kc(9, 100.0, 95.0, 100.0),    # prev 首根 → prev_4h 之高/低
                  ]
        raw_15m = [kc(20, 106.0, 105.0, 105.5),    # 15m high 106 > prev_high*1.008
                   kc(19, 105.0, 104.0, 105.0),
                   kc(18, 104.0, 103.0, 104.0)]
        events = []
        s3m._detect_failed_breakout('AUSDT', raw_4h, raw_15m, events)
        assert events == []                       # 第一次：进入 BREAKING_HIGH

        # 第二轮：回到突破下沿 → 确认失败
        raw_4h2 = [kc(11, 105.0, 95.0, 100.0), kc(10, 105.0, 95.0, 100.0)]
        raw_15m2 = [kc(21, 104.9, 104.0, 104.8),  # close 104.8 < 谁的 *0.998?
                    kc(20, 106.0, 102.0, 103.0),
                    kc(20, 105.0, 104.0, 104.0)]
        s3m._detect_failed_breakout('AUSDT', raw_4h2, raw_15m2, events)
        # breakout_high = state 记录 106.0 → close 104.8 < 106*0.998=105.79 → 失败
        assert len(events) == 1
        ev = events[0]
        assert ev['type'] == 'FAILED_BREAKOUT'
        assert ev['direction'] == 'HIGH'
        # strength = int(breakout_pct*10+20)，非负浮点→int 截断（真实公式）
        assert isinstance(ev['strength'], int)
        # rejected_from = state breakout_high (the 5m max at breakout)
        assert round(ev['rejected_from'], 4) == round(106.0, 4)

    def test_breakout_continuing_then_reset_to_idle(self, s3m, clock):
        def kc(t, h, l, c):
            return {'t': t, 'o': c, 'h': h, 'l': l, 'c': c, 'v': 0}
        raw_4h = [kc(10, 105.0, 95.0, 100.0), kc(9, 100.0, 95.0, 100.0)]
        raw_15m = [kc(20, 106.0, 104.0, 105.0), kc(19, 105.0, 104.0, 104.9),
                   kc(18, 104.0, 103.0, 103.9)]
        events = []
        s3m._detect_failed_breakout('AUSDT', raw_4h, raw_15m, events)
        assert s3m._fb_state['AUSDT']['state'] == 'BREAKING_HIGH'
        # 继续新高 > breakout_high*1.002 → 放弃（回 IDLE，不发事件）
        raw_15m2 = [kc(21, 107.0, 105.9, 106.9)]
        s3m._detect_failed_breakout('AUSDT', raw_4h, raw_15m2, events)
        assert s3m._fb_state['AUSDT']['state'] == 'IDLE'
        assert events == []

    def test_breaking_low_rejection(self, s3m, clock):
        def kc(t, h, l, c):
            return {'t': t, 'o': c, 'h': h, 'l': l, 'c': c, 'v': 0}
        raw_4h = [kc(10, 105.0, 95.0, 100.0), kc(9, 105.0, 100.0, 100.0)]
        raw_15m = [kc(20, 94.0, 93.9, 94.2), kc(19, 94.5, 93.5, 93.8)]
        events = []
        s3m._detect_failed_breakout('AUSDT', raw_4h, raw_15m, events)
        assert s3m._fb_state['AUSDT']['state'] == 'BREAKING_LOW'
        raw_4h2 = [kc(11, 105.0, 95.0, 100.0), kc(10, 105.0, 100.0, 100.0)]
        raw_15m2 = [kc(21, 95.0, 94.6, 94.85),    # close 反弹回 >95*1.002? =95.19 否
                    ]
        # close 94.85 > breakout_low 93.9 * 1.002 = 94.0878 → 失败（拒绝）
        s3m._detect_failed_breakout('AUSDT', raw_4h2, raw_15m2, events)
        assert len(events) == 1
        assert events[0]['direction'] == 'LOW'
        assert round(events[0]['breakdown_low'], 4) == 100.0  # prev_4h low


class TestMultiInstanceDedup:
    def test_inmemory_dedup_is_process_local(self, s3m, clock):
        """S3-4 冻结：同输入 + 各自进程内存 → 双实例都产生 ACTIVE（不 dedup）。"""
        # Instance A：
        s3m._update_event_state(pulse(), NOW)
        st_a = dict(s3m._event_states)
        # Instance B：模拟新进程（内存从零开始）处理同输入
        s3m._event_states.clear()
        out_b = s3m._update_event_state(pulse(), NOW)
        assert out_b['state'] == 'ACTIVE'          # 双发重复事件 — frozen
        assert dict(s3m._event_states) == st_a     # 同键同状态
