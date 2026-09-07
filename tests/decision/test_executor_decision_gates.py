"""Golden：open_position() 第二段 Decision Gates（shared_executor）。

目标：冻结"什么情况下 open_position 会阻止进入下单阶段"。
只替换 IO（Binance/Redis/analysis 存储/费率/余额）；业务逻辑（R:R、
熔断状态机、analysis gate 硬/软模式）全部走真实实现。
"""
import time

import pytest

from strategies import shared_executor as se


@pytest.fixture
def executor(monkeypatch, fake_redis):
    calls = {'post': [], 'get': [], 'pos_cache': [], 'enqueue': [], 'pg': []}

    monkeypatch.setattr(se, '_rget', fake_redis.get)
    monkeypatch.setattr(se, '_rset', fake_redis.set)
    monkeypatch.setattr(se, '_log', lambda *a, **k: None)
    monkeypatch.setattr(se, '_round_qty', lambda symbol, qty: qty)
    monkeypatch.setattr(se, '_algo_start_worker', lambda: None)

    def _enqueue(symbol, side, trigger, qty):
        calls['enqueue'].append({'symbol': symbol, 'side': side,
                                 'trigger': trigger, 'qty': qty})
    monkeypatch.setattr(se, '_algo_enqueue', _enqueue)
    monkeypatch.setattr(se, '_pg_record_event', lambda event: calls['pg'].append(event))

    def fapi_post(path, params=None):
        calls['post'].append({'path': path, 'params': params})
        if 'leverage' in path or 'marginType' in path:
            return {}
        qty = params.get('quantity', 0) if isinstance(params, dict) else 0
        return {'orderId': 1, 'status': 'FILLED', 'executedQty': str(qty),
                'avgPrice': '100.0'}
    monkeypatch.setattr(se, 'fapi_post', fapi_post)

    def fapi_get(path, params=None):
        calls['get'].append(path)
        if 'positionRisk' in path:
            return []                                       # 默认无已有仓位
        return None
    monkeypatch.setattr(se, 'fapi_get', fapi_get)

    monkeypatch.setattr(se, '_get_min_notional', lambda sym: 5.0)
    monkeypatch.setattr(se, '_get_funding_rate', lambda sym: 0.0001)
    monkeypatch.setattr(se, '_get_balance', lambda: 4000.0)
    monkeypatch.setattr(se, '_analysis_allows_open',
                        lambda *a, **k: (True, '', 1.0))
    # _drawdown_status 走真实实现（读 _get_balance + fake_redis peak/dd_pause）
    return {'se': se, 'calls': calls, 'redis': fake_redis}


def _open(**kw):
    base = {'name': 'S6', 'symbol': 'TESTUSDT', 'side': 'LONG',
            'entry_price': 100.0, 'stop_price': 95.0, 'qty': 100.0,
            'margin_mode': 'CROSSED', 'leverage': 3,
            'event_type': 'TREND_UP', 'strength': 70, 'tg_fn': None,
            'expected_move_pct': 0, 'decision_context': {}}
    base.update(kw)
    return base


def _run(executor, **kw):
    return executor['se'].open_position(**_open(**kw))


def test_strength_below_30_rejected(executor):
    """strength < 30 → 第 1 关即拒，无任何 IO。"""
    assert _run(executor, strength=25) is False
    assert executor['calls']['post'] == []


def test_strength_30_passes_to_execution(executor):
    """strength == 30 → 通过基础门槛（锁定 >= 边界）。"""
    assert _run(executor, strength=30) is True
    assert any(p['path'] == '/fapi/v1/order' for p in executor['calls']['post'])


def test_analysis_filter_hard_block(executor):
    """analysis 过滤 hard 拒单 → False，无下单。"""
    executor['se']._analysis_allows_open = (
        lambda *a, **k: (False, 'low quality', 0.5))
    assert _run(executor) is False
    order_posts = [p for p in executor['calls']['post'] if p['path'] == '/fapi/v1/order']
    assert order_posts == []


def test_analysis_filter_soft_scales_qty(executor, monkeypatch):
    """analysis 过滤 soft 模式 → qty × penalty（0.5），正常下单。"""
    executor['se']._analysis_allows_open = (
        lambda *a, **k: (False, 'bad follow', 0.5))
    monkeypatch.setenv('ANALYSIS_FILTER_MODE', 'soft')
    assert _run(executor) is True
    order = next(p for p in executor['calls']['post'] if p['path'] == '/fapi/v1/order')
    assert order['params']['quantity'] == pytest.approx(50.0)   # 100 × 0.5


def test_rr_below_min_rejected(executor):
    """R:R < 1.0 → REJECT。"""
    assert _run(executor, expected_move_pct=0.4, stop_price=99.5) is False
    order_posts = [p for p in executor['calls']['post'] if p['path'] == '/fapi/v1/order']
    assert order_posts == []


def test_rr_exactly_at_min_passes(executor):
    """R:R == 1.0 → 通过（锁定 >= 边界）。"""
    assert _run(executor, expected_move_pct=0.5, stop_price=99.5) is True


def test_drawdown_halt_rejected(executor):
    """回撤 ≥15%（halt）→ False。"""
    executor['redis'].set('account:peak', {'bal': 5000.0})
    assert _run(executor) is False                       # balance 4000 → dd 20%


def test_drawdown_reduced_scales_qty(executor):
    """回撤 ≥8% 但 <15% → 仓位 ×0.5。"""
    executor['redis'].set('account:peak', {'bal': 4400.0})
    assert _run(executor) is True
    order = next(p for p in executor['calls']['post'] if p['path'] == '/fapi/v1/order')
    assert order['params']['quantity'] == pytest.approx(50.0)   # 100 × 0.5


def test_recent_close_cooldown_rejected(executor):
    """4h 内平仓过 → False。"""
    executor['redis'].set('closed:TESTUSDT', {'ts': time.time()})
    assert _run(executor) is False


def test_funding_extreme_short_rejected(executor, monkeypatch):
    """SHORT 费率 < -0.1% → False。"""
    monkeypatch.setattr(se, '_get_funding_rate', lambda sym: -0.002)
    assert _run(executor, side='SHORT') is False


def test_funding_extreme_long_rejected(executor, monkeypatch):
    """LONG 费率 > +0.1% → False。"""
    monkeypatch.setattr(se, '_get_funding_rate', lambda sym: 0.002)
    assert _run(executor, side='LONG') is False


def test_exchange_existing_position_rejected(executor, monkeypatch):
    """交易所已有同 symbol 仓位 → False。"""
    def fapi_get(path, params=None):
        if 'positionRisk' in path:
            return [{'symbol': 'TESTUSDT', 'positionAmt': '50',
                     'entryPrice': '100.0'}]
        return None
    monkeypatch.setattr(se, 'fapi_get', fapi_get)
    assert _run(executor) is False


def test_normal_pass_full_flow(executor):
    """全 gate 通过 → 下单成功，PM 注册，PG event，Algo SL 入队。"""
    assert _run(executor) is True
    order = next(p for p in executor['calls']['post'] if p['path'] == '/fapi/v1/order')
    assert order['params']['side'] == 'BUY'
    assert order['params']['type'] == 'MARKET'
    assert order['params']['quantity'] == 100.0
    assert executor['calls']['pg']
    assert executor['calls']['enqueue']


def test_pause_open_file_rejected(executor):
    """PAUSE_OPEN 文件存在 → False（含清理，防泄漏）。"""
    from pathlib import Path
    pause = Path(se.__file__).parent / 'config/PAUSE_OPEN'
    try:
        pause.touch()
        assert _run(executor) is False
    finally:
        pause.unlink(missing_ok=True)
    assert not pause.exists()
