def test_window_includes_binance_taker_flow_features():
    from strategies.s3_orderflow import compute_window_data

    candles = []
    for i in range(25):
        volume = 10.0
        candles.append({
            't': i, 'o': 1.0, 'h': 1.1, 'l': 0.9, 'c': 1.0,
            'v': volume, 'tbv': 7.0,
        })

    data = compute_window_data(candles, 15, 'TESTUSDT')

    assert data['taker_buy_ratio'] == 0.7
    assert data['orderflow_bias'] == 0.4
