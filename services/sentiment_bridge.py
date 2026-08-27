#!/usr/bin/env python3
"""
sentiment_bridge.py — 免费市场情绪采集（S0 风险叠加数据源）
============================================================

采集两类公开、免费的市场情绪数据，写入 Redis `market:sentiment`，
供 S0 做「风险叠加」：市场过热（贪婪/多头拥挤）时收紧开仓。

  1. 恐慌贪婪指数 — https://api.alternative.me/fng/（真实市场，免费无 key）
  2. 全市场资金费率聚合 — 币安真盘公开端点 /fapi/v1/premiumIndex
     （公开数据，不受 demo/生产切换影响，永远是真实市场）

说明：
  - 资金费率是「多头拥挤度」的代理：均值显著为正 = 多头拥挤（易回落），
    显著为负 = 空头拥挤（易逼空）。
  - 当前未聚合 OI 总量（需逐 symbol 调 /openInterest），后续可按需补充。

启动: python3 services/sentiment_bridge.py   （默认每 5 分钟采样）
"""
import sys
import time
from pathlib import Path

import requests

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from shared.redis_store import set as _rset, get as _rget

SENTIMENT_KEY = 'market:sentiment'
FNG_URL = 'https://api.alternative.me/fng/'
FAPI_PUBLIC = 'https://fapi.binance.com'       # 真盘公开端点（公开数据）
FUNDING_PATH = '/fapi/v1/premiumIndex'

SAMPLE_INTERVAL_S = 300   # 5 分钟
LOG_DIR = _BASE.parent / 'logs' / 'sentiment'

# ── 风险阈值 ────────────────────────────────────────────────────────────────
FNG_GREED = 80        # 极度贪婪 → 过热
FNG_FEAR = 15         # 极度恐慌 → 恐慌
FUNDING_CROWDED = 0.0005   # 资金费率均值超过 ±0.05% → 单边拥挤


def _log(msg: str):
    line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] [sentiment] {msg}'
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f'{time.strftime("%Y%m%d")}.log').open('a').write(line + '\n')
    except Exception:
        pass


def fetch_fng() -> tuple[int, str]:
    """恐慌贪婪指数 → (value 0~100, label)。失败抛异常。"""
    data = requests.get(FNG_URL, timeout=10).json()
    item = data['data'][0]
    return int(item['value']), str(item.get('value_classification', ''))


def fetch_funding() -> dict:
    """全市场资金费率聚合（真盘公开数据，按 24h 成交额加权）。失败抛异常。

    简单平均会被大量冷门币（资金费率≈0）稀释，故用成交额加权，
    让 BTC/ETH/SOL 等主流币主导，反映真实的多头拥挤度。
    """
    tickers = requests.get(f'{FAPI_PUBLIC}/fapi/v1/ticker/24hr', timeout=10).json()
    vol = {}
    if isinstance(tickers, list):
        for t in tickers:
            if isinstance(t, dict) and str(t.get('symbol', '')).endswith('USDT'):
                try:
                    qv = float(t.get('quoteVolume', 0) or 0)
                except (TypeError, ValueError):
                    qv = 0.0
                vol[t['symbol']] = qv

    data = requests.get(f'{FAPI_PUBLIC}{FUNDING_PATH}', timeout=10).json()
    if not isinstance(data, list):
        raise RuntimeError('premiumIndex 响应异常')

    w_sum = w_total = 0.0
    pos = neg = 0
    for p in data:
        if not isinstance(p, dict):
            continue
        sym = str(p.get('symbol', ''))
        if not sym.endswith('USDT'):
            continue
        try:
            rate = float(p.get('lastFundingRate', 0) or 0)
        except (TypeError, ValueError):
            continue
        w = vol.get(sym, 0.0)
        w_sum += rate * w
        w_total += w
        if rate > 0.0001:
            pos += 1
        elif rate < -0.0001:
            neg += 1
    if w_total <= 0:
        raise RuntimeError('成交额加权失败（无成交量数据）')
    return {
        'avg_funding': round(w_sum / w_total, 8),
        'funding_pos': pos,
        'funding_neg': neg,
        'funding_sample': len(vol),
    }


def compute_sentiment(fng: int, avg_funding: float) -> dict:
    """由 F&G + 资金费率得出情绪风险叠加结论。"""
    sentiment_risk = fng >= FNG_GREED or fng <= FNG_FEAR or \
        avg_funding > FUNDING_CROWDED or avg_funding < -FUNDING_CROWDED
    if fng >= FNG_GREED:
        bias = 'greed'
    elif fng <= FNG_FEAR:
        bias = 'fear'
    else:
        bias = 'neutral'
    return {'sentiment_risk': sentiment_risk, 'bias': bias}


def sample() -> dict:
    fng, label = fetch_fng()
    fund = fetch_funding()
    overlay = compute_sentiment(fng, fund['avg_funding'])
    return {
        'ts': time.time(),
        'fng': fng,
        'fng_label': label,
        **fund,
        **overlay,
    }


def main():
    _log('sentiment_bridge 启动')
    while True:
        try:
            state = sample()
            _rset(SENTIMENT_KEY, state)
            _log(f"FNG={state['fng']}({state['fng_label']}) "
                 f"avgFunding={state['avg_funding']:.5%} "
                 f"risk={state['sentiment_risk']} bias={state['bias']}")
        except Exception as e:
            _log(f'采集失败（保留上次值）: {e}')
        time.sleep(SAMPLE_INTERVAL_S)


if __name__ == '__main__':
    main()
