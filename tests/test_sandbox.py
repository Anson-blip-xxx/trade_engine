"""scripts/sandbox 沙盘回归测试：市价单成交、条件单挂单、平仓清零、openorders 拦截顺序。"""
import pytest

import scripts.sandbox as sb


@pytest.fixture(autouse=True)
def _sandbox_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, 'STATE_FILE', tmp_path / 'sandbox_state.json')
    monkeypatch.setenv('SANDBOX', '1')
    sb.reset(5000)
    sb.seed_price('BTCUSDT', 60000.0)
    yield
    monkeypatch.delenv('SANDBOX', raising=False)


def test_is_active_env():
    assert sb.is_active() is True


def test_market_buy_updates_position():
    r = sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '0.05'})
    assert r['status'] == 'FILLED' and not r.get('code')
    risk = sb.mock_get_position_risk('BTCUSDT')
    assert float(risk[0]['positionAmt']) == pytest.approx(0.05)


def test_market_sell_opens_short():
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '0.02'})
    risk = sb.mock_get_position_risk('BTCUSDT')
    assert float(risk[0]['positionAmt']) == pytest.approx(-0.02)


def test_stop_order_held_as_open_order():
    r = sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_MARKET',
                            'quantity': '0.05', 'stopPrice': '58000'})
    assert r['status'] == 'NEW'
    assert len(sb.mock_get_open_orders('BTCUSDT')) == 1
    assert len(sb.mock_get_position_risk('BTCUSDT')) == 0  # 未成交


def test_cancel_order():
    r = sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_MARKET',
                            'quantity': '0.05', 'stopPrice': '58000'})
    c = sb.mock_cancel_order('BTCUSDT', r['orderId'])
    assert c['code'] == 0
    assert len(sb.mock_get_open_orders('BTCUSDT')) == 0


def test_close_position_clears():
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '0.05'})
    assert len(sb.mock_get_position_risk('BTCUSDT')) == 1
    sb._close_position('BTCUSDT')
    assert len(sb.mock_get_position_risk('BTCUSDT')) == 0


def test_account_has_assets_and_positions():
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '0.05'})
    acct = sb.mock_get_account()
    assert acct['assets'][0]['asset'] == 'USDT'
    assert len(acct['positions']) == 1


def test_openorders_intercept_order():
    """/fapi/v1/openOrders 必须走 mock_get_open_orders，而非 mock_post_order。"""
    from shared import binance_api as ba
    ba._sandbox_intercept('/fapi/v1/openOrders', {'symbol': 'BTCUSDT'})  # 不抛异常即可
    assert ba._sandbox_intercept('/fapi/v1/openOrders', {'symbol': 'BTCUSDT'}) == \
        sb.mock_get_open_orders('BTCUSDT')


def test_inactive_by_default(monkeypatch):
    monkeypatch.delenv('SANDBOX', raising=False)
    assert sb.is_active() is False


def test_check_positions_stop_loss_long():
    """多头止损触发：check_positions 自动平仓并清除条件单。"""
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '0.05'})
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_MARKET',
                        'quantity': '0.05', 'stopPrice': '59000'})
    sb.seed_price('BTCUSDT', 58500.0)
    closes = sb.check_positions()
    assert len(closes) == 1
    assert closes[0]['reason'] == '止损'
    assert len(sb.mock_get_position_risk('BTCUSDT')) == 0
    assert len(sb.mock_get_open_orders('BTCUSDT')) == 0


def test_check_positions_short_stop_hit():
    """空头止损触发：价格上涨打掉止损。"""
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '0.02'})
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'STOP_MARKET',
                        'quantity': '0.02', 'stopPrice': '60500'})
    sb.seed_price('BTCUSDT', 61000.0)
    assert len(sb.check_positions()) == 1
    assert len(sb.mock_get_position_risk('BTCUSDT')) == 0


def test_check_positions_no_trigger():
    """止损未触发：持仓与条件单都保留。"""
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '0.05'})
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_MARKET',
                        'quantity': '0.05', 'stopPrice': '59000'})
    sb.seed_price('BTCUSDT', 60000.0)
    assert sb.check_positions() == []
    assert len(sb.mock_get_position_risk('BTCUSDT')) == 1
    assert len(sb.mock_get_open_orders('BTCUSDT')) == 1


def test_reset_clears_state():
    """reset 清空持仓、条件单与 PnL 累计。"""
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '0.05'})
    sb.mock_post_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_MARKET',
                        'quantity': '0.05', 'stopPrice': '59000'})
    sb.reset(5000)
    assert len(sb.mock_get_position_risk('BTCUSDT')) == 0
    assert len(sb.mock_get_open_orders('BTCUSDT')) == 0
