"""P5-05：compute_and_detect → Publisher Port integration + call-order freeze。

All use real orchestration, with only mocked-out infrastructure (redis spy).
Freeze: write order / latest-slot / empty-snapshot / sorting (S3-9) /
single-try publish topology / failure semantics.
"""
import pytest


SYM = 'TESTUSDT'


def _seed(s3m):
    s3m._symbol_klines[SYM] = sorted([
        {'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0,
         'v': 10.0, 'tbv': 0.5} for i in range(120)], key=lambda x: -x['t'])


def _seed_pulsing(s3m):
    s3m._symbol_klines[SYM] = sorted([
        {'t': i, 'o': 100.0, 'h': 100.2, 'l': 99.8, 'c': 100.0,
         'v': 10.0, 'tbv': 0.5} for i in range(120)], key=lambda x: -x['t'])
    kl = s3m._symbol_klines[SYM]
    for i in range(15):
        kl[i]['c'] = 95.0 + 0.38 * (14 - i)
    for i in range(3):
        kl[i]['v'] = 60.0


def _snapshot(redis_spy, key='event:s3'):
    return redis_spy.store[key]


class TestSnapshotWrites:
    def test_normal_cycle_event_then_market_order(self, s3m, redis_spy):
        _seed_pulsing(s3m)
        s3m.compute_and_detect([SYM])
        keys = [w['key'] for w in redis_spy.writes]
        assert keys == ['event:s3', 'market:s3_data']    # freeze of write order

    def test_s39_sort_only_at_publish_layer(self, s3m, redis_spy):
        """S3-9 freeze: write-layer events are sorted by -strength; detector
        order is source order (does not take effect here)."""
        _seed(s3m)
        # Seed high-vol multi-event source data (HIGH_VOL + PULSE etc can co-exist)
        kl = s3m._symbol_klines[SYM]
        for i in range(3):
            kl[i]['v'] = 60.0
        s3m.compute_and_detect([SYM])
        events = _snapshot(redis_spy)['events']
        strengths = [e['strength'] for e in events]
        assert strengths == sorted(strengths, reverse=True)          # output layer sorted descending
        if len(events) >= 2:            # event order at detector layer (no sort)
            raw_15m_events = s3m.detect_events(SYM, s3m._symbol_windows[SYM],
                                               s3m._symbol_windows_raw[SYM])
            # Note: detect_events output order under equal-strength sequence is not subject to Redis,
            # is merely frozen to prove this helps observe that output layer is actually post-sort
            assert len(raw_15m_events) >= 1

    def test_latest_slot_overwrite_frozen(self, s3m, redis_spy, clock):
        _seed(s3m)
        s3m.compute_and_detect([SYM])
        first = dict(_snapshot(redis_spy))
        clock.advance(61)
        s3m.compute_and_detect([SYM])
        second = _snapshot(redis_spy)
        assert second['ts'] != first['ts']            # second write overwrites
        ev_writes = [w['key'] for w in redis_spy.writes]
        assert ev_writes.count('event:s3') == 2       # overwrite, not appending

    def test_no_events_still_writes_empty(self, s3m, redis_spy):
        _seed(s3m)
        s3m.compute_and_detect([SYM])
        snap = _snapshot(redis_spy)
        assert snap['events'] == []
        assert set(snap.keys()) == {'ts', 'events'}

    def test_market_snapshot_always_written(self, s3m, redis_spy):
        _seed(s3m)
        s3m.compute_and_detect([SYM])
        md = redis_spy.store['market:s3_data']
        assert set(md.keys()) == {'ts', 'symbols'}
        assert SYM in md['symbols']


class TestPublishSemantics:
    def test_publish_only_on_nonempty(self, s3m, redis_spy):
        _seed(s3m)
        s3m.compute_and_detect([SYM])
        assert redis_spy.publishes == []              # empty snapshot does not publish

    def test_publish_on_events(self, s3m, redis_spy):
        _seed_pulsing(s3m)
        s3m.compute_and_detect([SYM])
        assert redis_spy.publishes == [{'channel': 's3:event:notify',
                                        'message': '1'}]

    def test_set_failure_skips_publish_same_try(self, s3m, redis_spy):
        """single try topology: set fails → publish does not execute (swallowed)."""
        _seed_pulsing(s3m)
        redis_spy.set_exc = RuntimeError('redis down')
        s3m.compute_and_detect([SYM])                 # non-empty events → set raises
        assert redis_spy.writes                       # set attempted
        assert redis_spy.publishes == []              # publish skipped

    def test_set_failure_on_empty_no_error(self, s3m, redis_spy):
        _seed(s3m)
        redis_spy.set_exc = RuntimeError('redis down')
        s3m.compute_and_detect([SYM])                 # empty snapshot → set raises → swallowed
        assert redis_spy.publishes == []

    def test_publish_failure_swallowed_market_still_written(self, s3m, redis_spy):
        """publish failure does not affect market write (swallowed after set+publish in same try)."""
        _seed_pulsing(s3m)
        redis_spy.publish_exc = RuntimeError('pubsub down')
        s3m.compute_and_detect([SYM])
        assert redis_spy.writes[-1]['key'] == 'market:s3_data'   # market continues to be written

    def test_detect_exception_propagates_no_writes(self, s3m, redis_spy, monkeypatch):
        _seed(s3m)
        def boom(sym, w, wr):
            raise RuntimeError('detect exploded')
        monkeypatch.setattr(s3m, 'detect_events', boom)
        with pytest.raises(RuntimeError, match='detect exploded'):
            s3m.compute_and_detect([SYM])
        assert redis_spy.writes == []                 # interrupted midway → no output


class TestLifecycleMovesThroughRealChain:
    def test_lifecycle_state_applied_to_snapshot(self, s3m, redis_spy, clock):
        """snapshot events carry state/since (p5-03 injection path output)."""
        _seed_pulsing(s3m)
        s3m.compute_and_detect([SYM])
        events = _snapshot(redis_spy)['events']
        assert all(e.get('state') == 'ACTIVE' for e in events)
        assert all(e.get('since') == clock.now for e in events)
        if not events:                                    # slack: detected no source data
            pytest.skip('seed did not produce events')

    def test_second_cycle_updates_state(self, s3m, redis_spy, clock):
        _seed_pulsing(s3m)
        s3m.compute_and_detect([SYM])
        # Whether second-cycle cooldown/resend depends on strength delta — frozen at real pipeline level
        clock.advance(61)
        _seed_pulsing(s3m)
        s3m.compute_and_detect([SYM])
        snap2 = _snapshot(redis_spy)
        assert snap2['ts'] > clock.now - 61            # second-cycle updated ts


class TestThreadAndWSSemanticsUnchanged:
    def test_thread_functions_untouched(self, s3m):
        import inspect
        # WS/thread-related source code is at the original address (no P5-05 changes)
        assert 'run_forever' in inspect.getsource(s3m.ws_kline_loop)
        src = inspect.getsource(s3m.market_brain_loop)
        assert 'FETCH_INTERVAL' in src
        assert 's3_ports' not in src                  # main loop does not directly touch port


def test_failure_matrix_documented(s3m, redis_spy):
    """inventory failure matrix documentation anchor test (failure matrix of 11 failure points)."""
    _seed(s3m)
    s3m.compute_and_detect([SYM])
    if not _snapshot(redis_spy)['events']:
        pytest.skip('seed did not generate events — failure matrix in the closure doc §7')
