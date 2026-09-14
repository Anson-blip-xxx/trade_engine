

def test_ledger_port_shim_wiring_frozen(recorder_env=None):
    """P7-03B 的 record_trade wrapper 是 thin delegation via service —
    legacy helpers（loss cooldown / dup check / partial key）仍保留原址。"""
    import inspect
    src = inspect.getsource(
        __import__('shared.trade_recorder', fromlist=['x']).record_trade)
    assert 'PositionLedgerService' in src
    assert '_partial_key' in src                 # helper 不搬
    assert '_is_duplicate_record' in src
