#!/usr/bin/env python3
"""
tv_bridge.py — TradingView Webhook → 内部事件总线桥
====================================================

职责（只做信号转发，不下单、不碰持仓）：
  1. 接收 TradingView alert 的 webhook POST（JSON）
  2. 校验 secret / 幂等去重 / 信号名映射 / 标的规范化
  3. 写入 Redis `event:tv` 快照（与 `event:s3` 同构）
  4. 发布 s3:event:notify 唤醒 S6/S8 执行器（走全部现有闸门与 PM 风控）

Pine 端 alert 消息规范（JSON 字符串）：
  {"secret":"xxx","signal":"TREND_UP_LONG","symbol":"{{ticker}}",
   "price":{{close}},"strength":70,"comment":"..."}

启动: python3 services/tv_bridge.py   (默认 0.0.0.0:8001)
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from shared.redis_store import get as _rget, set as _rset, publish as _rpublish
from shared.binance_api import load_config

# ── 配置 ────────────────────────────────────────────────────────────────
LOG_DIR = _BASE.parent / 'logs' / 'tv'
TV_EVENT_KEY = 'event:tv'
TV_DEDUP_KEY = 'tv:dedup'
NOTIFY_CHANNEL = 's3:event:notify'     # 与 S3 共用唤醒频道
SNAPSHOT_MAX_EVENTS = 20               # 快照内最多保留事件数
SNAPSHOT_WINDOW_S = 90                 # 与 read 侧新鲜期一致
DEDUP_WINDOW_S = 300                   # 同 (signal, symbol) 5 分钟内去重
DEFAULT_STRENGTH = 50

# TV 信号名 → (内部 event_type, 方向)
# 内部类型沿用 S3 词表，S6/S8 的现有闸门（强度门槛/regime 门控/趋势过滤）自动生效
TV_SIGNAL_MAP = {
    # 多头
    'TREND_UP_LONG': ('TREND_UP', 'LONG'),
    'PULSE_UP_LONG': ('PULSE_UP', 'LONG'),
    'VIOLENT_LONG': ('VIOLENT_BULLISH', 'LONG'),
    'PUMP_LONG': ('PUMP_UP', 'LONG'),
    # 空头
    'TREND_DOWN_SHORT': ('TREND_DOWN', 'SHORT'),
    'PULSE_DOWN_SHORT': ('PULSE_DOWN', 'SHORT'),
    'VIOLENT_SHORT': ('VIOLENT_BEARISH', 'SHORT'),
    'PANIC_SELL_SHORT': ('PANIC_SELL', 'SHORT'),
    'PUMP_SHORT': ('PUMP_DOWN', 'SHORT'),
}


def _tvlog(msg: str):
    line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] [tv] {msg}'
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f'{time.strftime("%Y%m%d")}.log').open('a').write(line + '\n')
    except Exception:
        pass


def _secret() -> str:
    return load_config().get('TV_WEBHOOK_SECRET', '').strip()


def normalize_symbol(raw: str) -> str:
    """'BINANCE:BTCUSDT.P' / 'btcusdt' → 'BTCUSDT'"""
    sym = str(raw or '').strip().upper()
    if ':' in sym:
        sym = sym.split(':', 1)[1]
    if sym.endswith('.P'):
        sym = sym[:-2]
    return sym


def validate_and_normalize(payload: dict, now: float = None):
    """校验并归一化一条 alert。返回 (event, None) 或 (None, 拒绝原因)。"""
    now = now or time.time()
    if not isinstance(payload, dict):
        return None, 'payload 非对象'

    secret = str(payload.get('secret', ''))
    expected = _secret()
    if not expected:
        return None, '服务端未配置 TV_WEBHOOK_SECRET，拒绝所有信号'
    if secret != expected:
        return None, 'secret 校验失败'

    signal = str(payload.get('signal', '')).strip().upper()
    if signal not in TV_SIGNAL_MAP:
        return None, f'未知信号 {signal!r}（可用: {sorted(TV_SIGNAL_MAP)}）'

    symbol = normalize_symbol(payload.get('symbol', ''))
    if not symbol or not symbol.endswith('USDT') or '_PERP' in symbol:
        return None, f'非法标的 {symbol!r}（仅支持 *USDT 合约）'

    try:
        strength = float(payload.get('strength', DEFAULT_STRENGTH))
    except (TypeError, ValueError):
        strength = DEFAULT_STRENGTH
    strength = int(max(0, min(99, strength)))

    try:
        price = float(payload.get('price', 0) or 0)
    except (TypeError, ValueError):
        price = 0.0

    event_type, side = TV_SIGNAL_MAP[signal]
    event = {
        'type': event_type,
        'symbol': symbol,
        'strength': strength,
        'ts': now,
        'source': 'tv',
        'tv_signal': signal,
        'side': side,
    }
    if price > 0:
        event['price'] = price
    comment = str(payload.get('comment', '') or '').strip()
    if comment:
        event['comment'] = comment[:120]
    return event, None


def process_alert(payload: dict, now: float = None) -> tuple[bool, str]:
    """处理一条 alert：校验 → 去重 → 写快照 → 唤醒执行器。"""
    now = now or time.time()
    event, reason = validate_and_normalize(payload, now)
    if event is None:
        _tvlog(f'[拒绝] {reason}')
        return False, reason

    dedup_key = f"{event['tv_signal']}:{event['symbol']}"

    # ── 幂等去重（TV alert 重复触发防护）──
    dedup = _rget(TV_DEDUP_KEY) or {}
    if isinstance(dedup, dict):
        last = float(dedup.get(dedup_key, 0) or 0)
        if now - last < DEDUP_WINDOW_S:
            remain = int(DEDUP_WINDOW_S - (now - last))
            reason = f'{dedup_key} 去重窗口内重复（剩 {remain}s）'
            _tvlog(f'[去重] {reason}')
            return False, reason
        # 顺手清理过期项
        dedup = {k: v for k, v in dedup.items()
                 if isinstance(v, (int, float)) and now - float(v) < DEDUP_WINDOW_S}
    dedup[dedup_key] = now
    _rset(TV_DEDUP_KEY, dedup)

    # ── 追加进事件快照（与 event:s3 同构）──
    snapshot = _rget(TV_EVENT_KEY) or {}
    events = snapshot.get('events', []) if isinstance(snapshot, dict) else []
    events.append(event)
    events = [e for e in events if now - float(e.get('ts', 0)) <= SNAPSHOT_WINDOW_S]
    events = events[-SNAPSHOT_MAX_EVENTS:]
    _rset(TV_EVENT_KEY, {'ts': now, 'events': events})

    _rpublish(NOTIFY_CHANNEL)
    _tvlog(f'[接受] {event["symbol"]} {event["tv_signal"]} → '
           f'{event["type"]}/{event["side"]} str={event["strength"]}')
    return True, 'ok'


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 访问日志走 _tvlog，避免双写

    def _reply(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/healthz':
            self._reply(200, {'ok': True, 'ts': time.time()})
        else:
            self._reply(404, {'ok': False})

    def do_POST(self):
        if self.path not in ('/webhook', '/'):
            self._reply(404, {'ok': False, 'msg': 'not found'})
            return
        try:
            length = min(int(self.headers.get('Content-Length', 0) or 0), 64 * 1024)
            raw = self.rfile.read(length).decode('utf-8', errors='ignore')
            payload = json.loads(raw) if raw.strip() else {}
        except Exception as e:
            self._reply(400, {'ok': False, 'msg': f'bad json: {e}'})
            return
        try:
            ok, msg = process_alert(payload)
            self._reply(200 if ok else 200, {'ok': ok, 'msg': msg})
        except Exception as e:
            _tvlog(f'[异常] {e}')
            self._reply(500, {'ok': False, 'msg': str(e)})


def run_server(port: int = None):
    port = int(port or os.environ.get('TV_BRIDGE_PORT', 8001))
    srv = ThreadingHTTPServer(('0.0.0.0', port), _Handler)
    _tvlog(f'tv_bridge 启动 :{port} （POST /webhook, GET /healthz）')
    if not _secret():
        _tvlog('[警告] TV_WEBHOOK_SECRET 未配置，将拒绝所有信号（fail closed）')
    srv.serve_forever()


if __name__ == '__main__':
    run_server()
