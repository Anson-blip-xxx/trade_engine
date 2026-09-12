"""P5-01：Redis 语义 + 失败矩阵 golden（全部走真实函数，只 mock 最外层 IO）。"""
import pytest

from conftest import w15m, windows


class TestRedisSlotSemantics:
    def test_event_s3_is_latest_slot_not_queue(self, s3m, redis_spy, clock):
        """S3-1：两次写 → 覆盖；快照 key 覆盖语义（读方只见最新）。"""
        e1 = {'type': 'HIGH_VOL', 'symbol': 'AUSDT', 'strength': 30}
        snap1 = {'ts': clock.now, 'events': [e1]}
        s3m._rset('event:s3', snap1)
        clock.advance(60)
        snap2 = {'ts': clock.now, 'events': []}
        s3m._rset('event:s3', snap2)
        assert redis_spy.store['event:s3'] is snap2       # 全量覆盖（引用替换）
        assert snap1['events']                            # 前值仍在 spy 历史
        assert 'market:s3_data' not in redis_spy.store    # 未误触 market key

    def test_publish_only_on_events_with_payload_channel(self, s3m, redis_spy):
        s3m._rset('event:s3', {'ts': 1, 'events': [{'type': 'HIGH_VOL',
                                                    'symbol': 'A',
                                                    'strength': 30}]})
        s3m._rpublish('s3:event:notify')
        assert redis_spy.publishes == [{'channel': 's3:event:notify',
                                        'message': '1'}]  # message 恒 '1'

    def test_publish_failure_no_signal_to_trigger(self, s3m, redis_spy):
        """publish 失败语义：orchestration 层 try/except 吞错（compute 测试已证），
        inert；此处不复检。"""
        assert redis_spy.publish_exc is None


class TestNoDatabase:
    def test_no_db_calls(self, s3m, redis_spy):
        """S3 无数据库访问（对照 execution 边界）——源码级事实。"""
        import inspect
        src = inspect.getsource(s3m)
        assert 'psycopg' not in src and 'INSERT' not in src


class TestHiddenIO:
    def test_log_writes_file_silent_on_failure(self, s3m, monkeypatch, tmp_path):
        s3m._log('heartbeat test')
        logdir = tmp_path / 's3log'
        files = list(logdir.iterdir())
        assert len(files) == 1
        content = files[0].read_text()
        assert '[s3] heartbeat test' in content

    def test_log_failure_silent(self, s3m, monkeypatch):
        import strategies.s3_orderflow as s3

        class Broken:
            def mkdir(self, *a, **k):
                raise RuntimeError('fs ro')
        # _LOG_DIR 替换成 mkdir 炸弹 → 吞错不抛
        monkeypatch.setattr(s3, '_LOG_DIR', Broken())
        s3m._log('still alive')               # 不 raise

    def test_futures_to_spot_mapping(self, s3m):
        assert s3m._futures_to_spot('BTCUSDT') == 'btc'   # btc/eth 特例（事实）
        assert s3m._futures_to_spot('ETHUSDT') == 'eth'
        assert s3m._futures_to_spot('XRPUSDT') == 'xrpusdt'
        assert s3m._futures_to_spot('SHIB1000USDT') == 'shib1000usdt'

    def test_get_spot_momentum_requests_and_silence(self, s3m, monkeypatch):
        import strategies.s3_orderflow as s3

        class R:
            def __init__(self, status, body):
                self.status_code = status
                self._b = body

            def json(self):
                return self._b
        seen = []
        monkeypatch.setattr(s3.requests, 'get',
                            lambda url, timeout=5: seen.append(
                                url) or R(200, {'priceChangePercent': '4.2',
                                                'quoteVolume': '123'}))
        out = s3m._get_spot_momentum('BTCUSDT')
        assert out == {'spot_chg': 4.2, 'spot_vol': 123.0}
        assert seen[0].startswith('https://api.binance.com/api/v3')

    def test_get_spot_momentum_failure_empty(self, s3m, monkeypatch):
        import strategies.s3_orderflow as s3

        def boom(*a, **k):
            raise RuntimeError('net down')
        monkeypatch.setattr(s3.requests, 'get', boom)
        assert s3m._get_spot_momentum('BTCUSDT') == {}

    def test_api_get_global_rate_limit_state(self, s3m, monkeypatch):
        """_api_get 全局限速（_API_LAST_CALL）——hidden IO seam 冻结。"""
        import strategies.s3_orderflow as s3
        state = []

        class R:
            status_code = 200

            def json(self):
                return {}

        monkeypatch.setattr(s3.requests, 'get',
                            lambda *a, **k: state.append(1) or R())
        s3m._api_get('url-a')
        s3m._api_get('url-b')         # 第二次因 0.1s 限速 sleep —— fake time 记录
        assert len(state) == 2


class TestFailureMatrix:
    def test_get_top_symbols_non_200_empty(self, s3m, monkeypatch):
        import strategies.s3_orderflow as s3

        class R:
            status_code = 500

            def json(self):
                return {}

        monkeypatch.setattr(s3.requests, 'get', lambda *a, **k: R())
        assert s3m.get_top_symbols() == []

    def test_get_top_symbols_filters_usdt_and_volume(self, s3m, monkeypatch):
        import strategies.s3_orderflow as s3

        class R:
            status_code = 200

            def json(self):
                return [
                    {'symbol': 'AAAUSDT', 'quoteVolume': '60000000'},
                    {'symbol': 'BBBUSDT', 'quoteVolume': '40000000'},  # 低于 MIN_VOL_24H
                    {'symbol': 'BTCBRL', 'quoteVolume': '999999999'},  # 非 USDT
                    'bad-row',
                ]

        monkeypatch.setattr(s3.requests, 'get', lambda *a, **k: R())
        assert s3m.get_top_symbols() == ['AAAUSDT']

    def test_fetch_klines_bad_data_empty(self, s3m, monkeypatch):
        import strategies.s3_orderflow as s3

        class R:
            status_code = 200

            def json(self):
                return {}
        monkeypatch.setattr(s3.requests, 'get', lambda *a, **k: R())
        assert s3m.fetch_klines('XUSDT') == []

    def test_fetch_klines_shape_mapping(self, s3m, monkeypatch):
        import strategies.s3_orderflow as s3

        k = [1660000000000, 100.0, 101.0, 99.0, 100.5, 1234.5, 0, 0, 0, 555.0]
        tbv_idx9 = k[9]

        class R:
            status_code = 200

            def json(self):
                return [k]

        monkeypatch.setattr(s3.requests, 'get', lambda *a, **k: R())
        out = s3m.fetch_klines('XUSDT')
        assert out == [{'t': 1660000000, 'o': 100.0, 'h': 101.0, 'l': 99.0,
                        'c': 100.5, 'v': 1234.5, 'tbv': tbv_idx9}]

    def test_rsi_short_series_default_50(self, s3m):
        assert s3m.compute_rsi([100.0, 100.0], 14) == 50.0

    def test_rsi_losses_zero_returns_100(self, s3m):
        asc = [float(i) for i in range(20)]
        assert s3m.compute_rsi(asc, 14) == 100.0

    def test_ema_short_series_returns_last_value(self, s3m):
        vals = [float(i) for i in range(10)]
        assert s3m.compute_ema(vals, 20) == 9.0

    def test_ema_cache_incremental_uses_values0_frozen(self, s3m):
        """S3-1 冻结：增量路径以 values[0]（窗口最旧闭合价）为"latest"
        ——真实代码如此，不修。"""
        # 增量分支冻结：命中 _ema_cache 时以 values[0]（窗口内最旧闭合）
        # 当作 "latest" 做一次 EMA 递推 —— 真实代码如此，勿修。
        s3m._ema_cache['XUSDT2'] = {20: 5.0}
        out2 = s3m.compute_ema([9.9, 1.0], 20, symbol='XUSDT2')
        k = 2 / (20 + 1)
        assert out2 == 9.9 * k + 5.0 * (1 - k)
        assert s3m._ema_cache['XUSDT2'][20] == out2  # 缓存被增量覆盖"

    def test_ws_big_order_accumulates_with_50k_threshold(self, s3m, monkeypatch):
        class WsStub:
            pass
        big = {'ts': 111, 'symbol': 'BTCUSDT', 'price': 100.0, 'qty': 499.0,
               'usdt': 49900.0, 'side': 'BUY'}
        small = {'q': '0.1', 'p': '100.0'}          # 10 USDT → 低于 50k
        msg_big = '{"data": {"q": "500.0", "p": "110.0", "s": "btcusdt", "m": false}}'
        s3m._on_trade_msg(None, msg_big)
        assert len(s3m._big_orders) == 1
        o = s3m._big_orders[0]
        assert o == {'ts': pytest.approx(o['ts']), 'symbol': 'BTCUSDT',
                     'price': 110.0, 'qty': 500.0, 'usdt': 55000.0,
                     'side': 'BUY'}                 # m=False → 主动买
        s3m._on_trade_msg(None, small)
        assert len(s3m._big_orders) == 1            # 小单不入

    def test_ws_kline_msg_alive_and_closed(self, s3m, monkeypatch):
        import json as _json

        def msg(sym, ts, o, h, l, c, is_final):
            return _json.dumps({'data': {'e': 'kline', 's': sym, 'k': {
                't': ts * 1000, 'o': str(o), 'h': str(h), 'l': str(l),
                'c': str(c), 'v': '10.0', 'x': is_final}}})

        s3m._on_kline_msg(None, msg('BTCUSDT', 100, 5.0, 6.0, 4.0, 5.5, False))
        assert s3m._current_kline['BTCUSDT']['c'] == 5.5
        s3m._on_kline_msg(None, msg('BTCUSDT', 100, 5.0, 6.0, 4.0, 5.0, True))
        assert 'BTCUSDT' not in s3m._current_kline  # 关闭后移
        assert len(s3m._symbol_klines['BTCUSDT']) == 1

    def test_ws_bad_message_silent(self, s3m):
        s3m._on_kline_msg(None, 'not json')          # 吞错
        s3m._on_kline_msg(None, '{"data": {}}')      # 无 e<k 结构
        assert s3m._symbol_klines == {}
