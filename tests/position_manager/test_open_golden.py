"""Golden：PM 开仓（open_position）。

冻结当前行为，包括"同 symbol 重复 open 时订单已发出但持仓记录不覆盖"这一
Observed Current Behavior。
"""
from pm_golden_helpers import make_position


def test_open_first_long_saves_full_position_record(pm_full):
    """首笔开多：返回 True，持仓记录字段完整冻结，AlgoSL 入队，市价单发出。"""
    pm_full['set_sandbox'](True)
    ok = pm_full['pm'].open_position(
        'AUSDT', 'LONG', 1.0, 100.0, 3, 0.92,
        atr=0.01, score=66, signal_type='TREND_UP', system='S6',
        margin_type='CROSSED')
    assert ok is True

    stored = pm_full['redis'].get('pm:positions')
    pos = stored['AUSDT']
    assert pos['entry'] == 1.0
    assert pos['qty'] == 100.0 and pos['original_qty'] == 100.0
    assert pos['leverage'] == 3
    assert pos['sl'] == 0.92
    assert pos['side'] == 'LONG' and pos['system'] == 'S6'
    assert pos['signal_type'] == 'TREND_UP' and pos['score'] == 66
    assert pos['margin_type'] == 'CROSSED'
    assert pos['be_done'] is False and pos['tp_done'] == []
    assert pos['highest'] == 1.0 and pos['lowest'] == 1.0
    assert pos['algo_sl_id'] is None          # worker 异步回填，open 时刻为 None
    assert isinstance(pos['open_time'], int) and pos['open_time'] > 0

    # 下单顺序：杠杆 → 保证金模式 → 市价单
    posts = pm_full['calls']['post']
    assert [p['path'] for p in posts] == ['/fapi/v1/leverage',
                                          '/fapi/v1/marginType',
                                          '/fapi/v1/order']
    order_params = posts[-1]['params']
    assert order_params['side'] == 'BUY' and order_params['type'] == 'MARKET'
    assert order_params['quantity'] == 100.0

    # AlgoSL 入队：多单止损方向为 SELL
    enq = pm_full['calls']['enqueue']
    assert enq == [{'symbol': 'AUSDT', 'side': 'SELL', 'trigger': 0.92, 'qty': 100.0}]


def test_open_short_defaults(pm_full):
    """开空：订单方向 SELL，AlgoSL 方向 BUY，highest/lowest 均为 entry。"""
    pm_full['set_sandbox'](True)
    ok = pm_full['pm'].open_position(
        'BUSDT', 'SHORT', 2.0, 50.0, 5, 2.16,
        signal_type='TREND_DOWN', system='S8', margin_type='ISOLATED')
    assert ok is True
    pos = pm_full['redis'].get('pm:positions')['BUSDT']
    assert pos['side'] == 'SHORT' and pos['system'] == 'S8'
    assert pos['highest'] == 2.0 and pos['lowest'] == 2.0
    posts = pm_full['calls']['post']
    assert posts[-1]['params']['side'] == 'SELL'
    assert pm_full['calls']['enqueue'][0]['side'] == 'BUY'
    assert pm_full['calls']['enqueue'][0]['qty'] == 50.0


def test_open_same_symbol_order_placed_but_record_not_overwritten(pm_full):
    """Observed Current Behavior：同 symbol 再次 open 时——
    市价单已经发出（第 3 个 post 照常执行），但 pm:positions 里的持仓记录
    保持原值不被覆盖，函数返回 True。
    Potential concern: 可能造成交易所实际仓位与 PM 记录不一致。
    Future phase: Phase 7 PM decomposition。"""
    pm_full['set_sandbox'](True)
    existing = make_position(entry=2.0, qty=10.0, original_qty=10.0)
    pm_full['redis'].set('pm:positions', {'AUSDT': existing})

    ok = pm_full['pm'].open_position(
        'AUSDT', 'LONG', 1.0, 100.0, 3, 0.92, system='S6', signal_type='TREND_UP')

    assert ok is True                                   # 当前行为：仍返回 True
    posts = pm_full['calls']['post']
    assert posts[-1]['path'] == '/fapi/v1/order'        # 订单确实已发出
    stored = pm_full['redis'].get('pm:positions')['AUSDT']
    assert stored['entry'] == 2.0 and stored['qty'] == 10.0   # 记录未覆盖


def test_open_rejected_when_closed_recently(pm_full):
    """4h 内被平仓过的 symbol → 开仓直接拒绝，不下单。"""
    import time
    pm_full['set_sandbox'](True)
    pm_full['redis'].set('closed:AUSDT', {'ts': time.time()})
    ok = pm_full['pm'].open_position(
        'AUSDT', 'LONG', 1.0, 100.0, 3, 0.92, system='S6')
    assert ok is False
    assert pm_full['calls']['post'] == []
    assert 'pm:positions' not in pm_full['redis'].data or \
        'AUSDT' not in (pm_full['redis'].get('pm:positions') or {})


def test_open_order_exception_returns_false_without_save(pm_full):
    """市价单异常 → 返回 False，不写持仓记录。"""
    pm_full['set_sandbox'](True)

    def _raise(path, params=None):
        if path == '/fapi/v1/order':
            raise RuntimeError('network down')
        return {'orderId': 1}

    pm_full['set_s6api'](fapi_post=_raise)
    ok = pm_full['pm'].open_position(
        'AUSDT', 'LONG', 1.0, 100.0, 3, 0.92, system='S6')
    assert ok is False
    assert pm_full['redis'].get('pm:positions') is None


def test_open_metadata_overrides_landed_in_position(pm_full):
    """metadata / reasons 字典的键直接并入持仓记录（当前行为）。"""
    pm_full['set_sandbox'](True)
    ok = pm_full['pm'].open_position(
        'AUSDT', 'LONG', 1.0, 100.0, 3, 0.92,
        metadata={'position_id': 'custom-pid', 'my_tag': 'x'},
        reasons={'reason_code': 'SIGNAL_A'})
    assert ok is True
    pos = pm_full['redis'].get('pm:positions')['AUSDT']
    assert pos['position_id'] == 'custom-pid'
    assert pos['my_tag'] == 'x'
    assert pos['reason_code'] == 'SIGNAL_A'


def test_open_position_id_generated_when_absent(pm_full):
    """未提供 position_id → open 不生成（生成发生在 _merge/_close 时）。"""
    pm_full['set_sandbox'](True)
    pm_full['pm'].open_position('AUSDT', 'LONG', 1.0, 100.0, 3, 0.92, system='S6')
    pos = pm_full['redis'].get('pm:positions')['AUSDT']
    assert 'position_id' not in pos      # 当前行为：open 阶段不写 position_id
