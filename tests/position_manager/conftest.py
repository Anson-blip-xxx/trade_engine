"""PM Golden Tests 共享夹具（P1-04）。

隔离原则：只替换外部基础设施（Redis / Binance API / Postgres / Telegram / Algo 队列线程），
保留 PM 的真实核心逻辑：_load / _save / _merge_meta / _close / _partial_close /
open_position / _mark_closed / _was_closed_recently / _clear_closed_marker。

禁止在本目录测试中修改 PM 的任何业务逻辑。
"""
import pytest


@pytest.fixture
def pm_storage(monkeypatch, fake_redis):
    """轻量隔离：只替换 Redis 读写、日志、pg 事件；其余 PM 函数全部真实。

    注意：必须同时替换 redis_store.delete——_clear_closed_marker 内部惰性
    `from shared.redis_store import delete`，绕过了 pm._rget/_rset 注入缝
    （Observed Current Behavior，见 docs/v2/PM_GOLDEN_OBSERVATIONS.md）。
    """
    from shared import position_manager as pm

    pg_events = []
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    monkeypatch.setattr(pm, '_pg_record_event', lambda event: pg_events.append(event))
    return {'pm': pm, 'redis': fake_redis, 'pg_events': pg_events}


@pytest.fixture
def pm_full(monkeypatch, fake_redis):
    """完整隔离：Redis + s6api(Binance) + Algo 队列线程 + pg。

    保留真实：_load/_save/_merge_meta/_close/_partial_close/open_position/
    _mark_closed/_was_closed_recently/_clear_closed_marker/_round_qty/_get_cfg。
    """
    from shared import position_manager as pm

    calls = {'post': [], 'record_trade': [], 'enqueue': [], 'pg': [], 'logs': [],
             'cancel_algo': []}

    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    # _clear_closed_marker 惰性导入 redis_store.delete 直连真 Redis（见观察文档）
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_pmlog', lambda msg: calls['logs'].append(str(msg)))
    monkeypatch.setattr(pm, '_pg_record_event', lambda event: calls['pg'].append(event))
    monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)

    def _enqueue(symbol, side, trigger, qty):
        calls['enqueue'].append({'symbol': symbol, 'side': side,
                                 'trigger': trigger, 'qty': qty})
    monkeypatch.setattr(pm, '_algo_enqueue', _enqueue)
    monkeypatch.setattr(pm, '_cancel_all_algo',
                        lambda symbol: calls['cancel_algo'].append(symbol))

    def make_s6api(fapi_get=None, fapi_post=None, record_trade=None,
                   get_symbol_info=None, get_price=None):
        def _default_post(path, params=None):
            calls['post'].append({'path': path, 'params': params})
            return {'orderId': 1, 'status': 'FILLED', 'executedQty': '100'}

        def _s6api():
            return (
                fapi_get or (lambda path, params=None: []),
                fapi_post or _default_post,
                lambda *a, **k: {},
                get_price or (lambda sym: 100.0),
                get_symbol_info or (lambda sym: (6, 6)),
                lambda *a, **k: (0, 0, 0),
                lambda *a, **k: 50.0,
                record_trade or (lambda *a, **kw: calls['record_trade'].append(
                    {'args': a, 'kwargs': kw})),
            )
        return _s6api

    def set_s6api(**kw):
        monkeypatch.setattr(pm, '_s6api', make_s6api(**kw))

    def set_sandbox(active: bool):
        monkeypatch.setattr(pm, '_sandbox_active', lambda: active)

    set_s6api()
    set_sandbox(False)
    return {'pm': pm, 'redis': fake_redis, 'calls': calls,
            'set_s6api': set_s6api, 'set_sandbox': set_sandbox}
