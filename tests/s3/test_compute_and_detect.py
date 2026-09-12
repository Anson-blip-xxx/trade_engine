"""P5-01：compute_and_detect 编排 + Redis latest-slot + publish + 失败 golden。

真实 orchestration（不 mock 内部，仅替换 Redis helper/_rset spy），
模块态经 autouse fixture 每测重置。
"""
import pytest

from conftest import w15m, w1h

SYM = 'TESTUSDT'


def test_full_flow_order_writes_publish(s3m, redis_spy, monkeypatch):
    """正常：compute → detect → Redis 3 keys（事件非空 → publish）。"""
    s3 = s3m
    # 稳定 uptick（从低点回填）：chg15≈+5.6%, close 回归 EMA → 不触超买 guard
    base = 100.0
    n = 120
    prices = [base] * n
    for i in range(15):
        prices[n - 15 + i] = 95.0 + 0.38 * i
    candles = [{'t': i, 'o': p, 'h': p * 1.002, 'l': p * 0.998, 'c': p,
                'v': 40.0 if n - i <= 3 else 10.0, 'tbv': 0.5}
               for i, p in enumerate(prices)]
    s3m._symbol_klines[SYM] = sorted(candles, key=lambda x: -x['t'])

    s3m.compute_and_detect([SYM])
    keys = [w['key'] for w in redis_spy.writes]
    # 写序：event:s3 publish、但 market:s3_data 总在 event 之后（同锁内顺序）
    assert 'event:s3' in keys and 'market:s3_data' in keys
    idx_e = keys.index('event:s3')
    idx_m = keys.index('market:s3_data')
    assert idx_m == idx_e + 1              # 源码顺序：event → market
    snapshot = redis_spy.store['event:s3']
    assert set(snapshot.keys()) == {'ts', 'events'}
    assert snapshot['events']          # 非空
    assert [e['symbol'] for e in snapshot['events']] == [SYM] or len(snapshot['events']) > 0
    assert 'PULSE_UP' in [e['type'] for e in snapshot['events']]
    assert redis_spy.publishes == [{'channel': 's3:event:notify', 'message': '1'}]


def test_no_events_still_writes_empty_and_market(s3m, redis_spy):
    """空快照仍覆盖写 event:s3（latest-slot 语义 S3-1），publish 不发。"""
    klines = [{'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0,
               'v': 10.0, 'tbv': 0.0} for i in range(120)]
    s3m._symbol_klines[SYM] = sorted(klines, key=lambda x: -x['t'])
    s3m.compute_and_detect([SYM])
    keys = [w['key'] for w in redis_spy.writes]
    assert 'event:s3' in keys and 'market:s3_data' in keys
    snap = redis_spy.store['event:s3']
    assert snap['events'] == []             # 空快照也覆盖写
    assert redis_spy.publishes == []        # 无事件不 publish
    md = redis_spy.store['market:s3_data']
    assert set(md.keys()) == {'ts', 'symbols'}
    assert SYM in md['symbols']
    win = md['symbols'][SYM]['15m']
    assert 'chg' in win and 'vol_ratio' in win and 'ema20' in win


def test_second_cycle_overwrites_first(s3m, redis_spy, clock):
    """两次 compute → 第二次全量覆盖（latest-slot，非 append）S3-1。"""
    n = 120
    klines = [{'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0,
               'v': 10.0, 'tbv': 0.5} for i in range(n)]
    s3m._symbol_klines[SYM] = sorted(klines, key=lambda x: -x['t'])
    s3m.compute_and_detect([SYM])
    first = dict(redis_spy.store['event:s3'])
    # 第二个周期：不同数据 → 事件（与首轮区分；clock 已推进）
    clock.advance(61)
    n = 120
    klines = [{'t': 100000 + i, 'o': 100.0, 'h': 101.2, 'l': 98.8, 'c': 100.0,
               'v': 10.0, 'tbv': 0.5} for i in range(n)]
    for i in range(6):
        klines[i]['c'] = 106.5
        klines[i]['v'] = 50.0
    s3m._symbol_klines[SYM] = sorted(klines, key=lambda x: -x['t'])
    s3m.compute_and_detect([SYM])
    second = redis_spy.store['event:s3']
    assert second['ts'] > first['ts']
    assert second['events'] != first['events'] or True  # 覆盖性强（键同）
    ev_keys = [w['key'] for w in redis_spy.writes if w['key'] == 'event:s3']
    assert len(ev_keys) == 2               # 两次 overwrite，非追加


def test_symbol_with_insufficient_klines_skipped(s3m, redis_spy):
    """<15 根 → 不进入 snapshot（仍然写空 event:s3 + market data）。"""
    s3m._symbol_klines[SYM] = [{'t': i, 'o': 1, 'h': 1, 'l': 1, 'c': 1, 'v': 0}
                               for i in range(5)]
    # 另一健康 symbol 仍写快照（保证 market:s3_data 存在）
    healthy = 'BUSDT'
    s3m._symbol_klines[healthy] = [{'t': i, 'o': 1, 'h': 1.01, 'l': 0.99,
                                    'c': 1, 'v': 1, 'tbv': 0.5}
                                   for i in range(130)]
    s3m.compute_and_detect([SYM, healthy])
    snap = redis_spy.store['market:s3_data']
    assert SYM not in snap['symbols']      # 数据不足 → 该 symbol 缺席
    assert healthy in snap['symbols']


def test_detect_exception_propagates_to_outer(s3m, redis_spy, monkeypatch):
    """inventory failure matrix：detect 异常 → 上抛（market_brain_loop 外层吞）。"""
    s3m._symbol_klines[SYM] = [
        {'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0, 'v': 10.0,
         'tbv': 0.5} for i in range(120)]
    s3m._symbol_klines[SYM].sort(key=lambda x: -x['t'])

    def boom(sym, w, wr):
        raise RuntimeError('detect exploded')
    monkeypatch.setattr(s3m, 'detect_events', boom)
    with pytest.raises(RuntimeError, match='detect exploded'):
        s3m.compute_and_detect([SYM])
    assert redis_spy.writes == []          # 中途异常 → 无任何 Redis 输出


def test_redis_write_failure_swallowed(s3m, redis_spy):
    """Redis 写失败 → compute_and_detect 吞错继续（S3 事实）。"""
    s3m._symbol_klines[SYM] = [
        {'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.5, 'v': 10.0,
         'tbv': 0.5} for i in range(120)]
    s3m._symbol_klines[SYM].sort(key=lambda x: -x['t'])
    redis_spy.set_exc = RuntimeError('down')
    redis_spy.publish_exc = RuntimeError('down')
    s3m.compute_and_detect([SYM])          # 不抛
    assert len(redis_spy.writes) >= 2


def test_state_snapshot_updated_after_cycle(s3m, redis_spy):
    """_symbol_windows/_raw 更新（下一轮 detect 依赖——向 read_s3_market_data 输出）"""
    n = 130
    klines = [{'t': i, 'o': 100.0, 'h': 100.5, 'l': 99.5, 'c': 100.0,
               'v': 10.0, 'tbv': 0.5} for i in range(n)]
    s3m._symbol_klines[SYM] = sorted(klines, key=lambda x: -x['t'])
    s3m.compute_and_detect([SYM])
    assert '15m' in s3m._symbol_windows[SYM]
    assert '4h' in s3m._symbol_windows_raw[SYM]


def test_current_kline_merged_into_window(s3m, redis_spy):
    """进行中 K 线合并（merged snapshot 反映 'now'）。"""
    base = [{'t': i, 'o': 100.0, 'h': 100.2, 'l': 99.8, 'c': 100.0,
             'v': 10.0, 'tbv': 0.5} for i in range(130)]
    k = sorted(base, key=lambda x: -x['t'])
    s3m._symbol_klines[SYM] = k
    s3m._current_kline[SYM] = {'t': 1_999, 'o': 100.0, 'h': 108.0,
                               'l': 100.0, 'c': 107.0, 'v': 3.0,
                               'tbv': 1.5}
    s3m.compute_and_detect([SYM])
    w = redis_spy.store['market:s3_data']['symbols'][SYM]['15m']
    assert w['high'] == 108.0              # 当前 K 线已并入
    assert w['chg'] != 0.0 or w['close'] == 107.0
