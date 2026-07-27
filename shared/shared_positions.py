# stub — recreated after refactor
SHARED_BUDGET_PCT = 0.05
S6_POSITION_PCT = 0.08
S6_MIN_POSITION = 10.0

def score_to_fraction(score: float) -> float:
    return min(0.15, max(0.03, score / 100 * 0.15))

def is_open(symbol: str) -> bool:
    return False

def get_live_price(symbol: str) -> float:
    import requests as _req
    try:
        r = _req.get(f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}', timeout=5)
        return float(r.json()['price'])
    except Exception:
        return 0.0
