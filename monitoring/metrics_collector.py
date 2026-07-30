#!/usr/bin/env python3
"""
Prometheus metrics collector for trading systems.
Exposes :8000/metrics  — Prometheus scrapes every 15s.
Sends TG alerts when anomalies detected (max 1/hour per alert type).
"""
import json, os, re, sys, time, subprocess
from pathlib import Path
from prometheus_client import start_http_server, Gauge, Counter

# ── Paths ──
TRADE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR  = TRADE_DIR.parent / 'logs'

# ── Redis helpers ──
sys.path.insert(0, str(TRADE_DIR))
sys.path.insert(0, str(TRADE_DIR.parent))
from shared.redis_store import get as redis_get

# ── ClickHouse helpers ──
def ch_scalar(sql: str) -> str:
    try:
        from shared.clickhouse_client import query as _q
        r = _q(sql)
        if r and r[0] and r[0][0] is not None:
            return str(r[0][0])
        return ''
    except Exception:
        return ''

# ── TG alert ──
TG_TOKEN = ''
TG_CHAT_ID = ''
_LAST_ALERT = {}  # {alert_key: timestamp}

def _load_tg():
    global TG_TOKEN, TG_CHAT_ID
    if not TG_TOKEN:
        try:
            from shared.binance_api import load_config
            cfg = load_config()
            TG_TOKEN = cfg.get('TG_NOTIFY_TOKEN', '')
            TG_CHAT_ID = cfg.get('TG_CHAT_ID', '') or '5709781617'
        except Exception:
            pass

def tg_alert(key: str, text: str):
    """Send Telegram alert, max 1/hour per key"""
    now = time.time()
    last = _LAST_ALERT.get(key, 0)
    if now - last < 3600:
        return
    _LAST_ALERT[key] = now
    try:
        _load_tg()
        if not TG_TOKEN:
            print(f"[ALERT] {text}")
            return
        import requests
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': f'⚠️ {text}',
                  'parse_mode': 'Markdown'},
            timeout=10
        )
    except Exception as e:
        print(f"[TG alert failed] {key}: {e}")

# ════════════════════════════════════════════════════════
#  Define Metrics
# ════════════════════════════════════════════════════════
SYS_LABELS = ['system']

system_running = Gauge('sys_running', 'Systemd service up?', SYS_LABELS)
heartbeat_age  = Gauge('sys_heartbeat_age_seconds', 'Seconds since last heartbeat', SYS_LABELS)
error_count = Counter('sys_errors_total', 'Errors in logs', SYS_LABELS + ['severity'])

# S3 metrics (replaces old s2)
s3_signal_freshness = Gauge('s3_signal_freshness_seconds', 'Seconds since last S3 signal write')
s3_signal_count     = Gauge('s3_signal_count', 'S3 confirmed orderflow signals')
s3_pumping_count    = Gauge('s3_pumping_count', 'S3 pumping symbols detected')
s3_crashing_count   = Gauge('s3_crashing_count', 'S3 crashing symbols detected')
s3_pulse_freshness  = Gauge('s3_pulse_freshness_seconds', 'Seconds since last S3 pulse write')
s3_tracked_symbols  = Gauge('s3_tracked_symbols', 'S3 tracked symbols count')

s6b_positions = Gauge('s6b_positions', 'S6B open positions count')
s6b_pending   = Gauge('s6b_pending', 'S6B pending entry count')

position_count  = Gauge('positions_count', 'Open positions by system', SYS_LABELS)
wallet_balance  = Gauge('wallet_balance_usdt', 'Total wallet balance')
margin_used     = Gauge('margin_used_usdt', 'Total margin used')
budget_ratio    = Gauge('budget_ratio', 'Margin/balance ratio (0-1)')
daily_pnl       = Gauge('daily_pnl_usdt', 'Today PnL by system', SYS_LABELS)
daily_loss_breach = Gauge('daily_loss_breach', '1 if daily loss limit breached', SYS_LABELS)
system_paused   = Gauge('system_paused', '1 if new positions paused')
algo_orders_count = Gauge('algo_orders_count', 'Open conditional orders')

# ════════════════════════════════════════════════════════
#  Collectors
# ════════════════════════════════════════════════════════
LOGPAT = re.compile(r'\[([^\]]+)\]\s+\[([^\]]+)\]\s+(.*)')
_prev_error_count = {}

def collect_system_health():
    services = {
        's3':  's3-orderflow',
        's6':  'trading-engine-s6',
        's8':  'trading-engine-s8',
        's7':  's7-grid',
        's0':  's0-market-guard',
    }
    for name, svc in services.items():
        try:
            r = subprocess.run(['systemctl', 'is-active', svc],
                               capture_output=True, text=True, timeout=3)
            up = 1 if r.stdout.strip() == 'active' else 0
            system_running.labels(system=name).set(up)
            if up == 0:
                tg_alert(f'service_down_{name}', f'[{name.upper()}] 服务已停止')
        except Exception:
            system_running.labels(system=name).set(0)

def collect_heartbeat_age():
    now = time.time()
    # S6, S8A, S8B, S3, S6B
    hb_config = {
        's3':  (LOGS_DIR / 's3', ['[s3]'], 300),
        's6':  (LOGS_DIR / 's6', ['[S6]'], 120),
        's8':  (LOGS_DIR / 's8', ['[S8]'], 300),
    }
    for name, (log_dir, keywords, threshold) in hb_config.items():
        log_file = log_dir / time.strftime('%Y%m%d.log')
        if not log_file.exists():
            heartbeat_age.labels(system=name).set(-1)
            continue
        try:
            r = subprocess.run(['tail', '-50', str(log_file)],
                               capture_output=True, text=True, timeout=3)
            age = -1
            for line in reversed(r.stdout.strip().split('\n')):
                if any(k in line for k in keywords):
                    m = re.match(r'\[([\d-]+\s[\d:]+)\]', line)
                    if m:
                        ts = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
                        age = now - ts
                        break
            heartbeat_age.labels(system=name).set(age)
            if age > threshold:
                tg_alert(f'hb_timeout_{name}', f'[{name.upper()}] 心跳超时 {age:.0f}s')
        except Exception:
            heartbeat_age.labels(system=name).set(-1)

def collect_errors():
    now_ts = time.time()
    log_dirs = [('s6', LOGS_DIR / 's6'),
                ('s8', LOGS_DIR / 's8'),
                ('s3', LOGS_DIR / 's3')]
    for name, log_dir in log_dirs:
        log_file = log_dir / time.strftime('%Y%m%d.log')
        if not log_file.exists():
            continue
        try:
            r = subprocess.run(['tail', '-500', str(log_file)],
                               capture_output=True, text=True, timeout=3)
            errs = 0
            for line in r.stdout.strip().split('\n'):
                m = re.match(r'\[([\d-]+\s[\d:]+)\]', line)
                if m:
                    ts = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
                    if now_ts - ts > 300:
                        continue
                if 'ERROR' in line or '异常' in line or 'Traceback' in line:
                    errs += 1
                    error_count.labels(system=name, severity='error').inc()
                elif 'WARN' in line:
                    error_count.labels(system=name, severity='warn').inc()
            if errs >= 5:
                tg_alert(f'error_spike_{name}', f'[{name.upper()}] 异常飙升: {errs}次/5分钟')
        except Exception:
            pass

def collect_s3_signals():
    """S3 大单流信号 + 脉冲信号"""
    # 大单流
    try:
        data = redis_get('signal:s3_signals')
        if data:
            age = time.time() - data.get('ts', 0)
            s3_signal_freshness.set(age)
            s3_signal_count.set(len(data.get('signals', [])))
            if age > 1800:
                tg_alert('s3_stale', f'S3大单流已{age/60:.0f}分钟未更新')
        else:
            s3_signal_freshness.set(-1)
            s3_signal_count.set(0)
    except Exception:
        s3_signal_freshness.set(-1)
        s3_signal_count.set(0)

    # 追踪币种数
    try:
        data = redis_get('mover:s3_spot')
        if data:
            s3_tracked_symbols.set(len(data.get('movers', [])))
        else:
            s3_tracked_symbols.set(0)
    except Exception:
        s3_tracked_symbols.set(0)

# s6b_state removed (merged into S6)

def collect_positions():
    try:
        pm_data = redis_get('pm:positions')
        if pm_data:
            counts = {}
            for sym, pos in pm_data.items():
                sys_name = pos.get('system', 'unknown')
                counts[sys_name] = counts.get(sys_name, 0) + 1
            for sys_name in ('s6', 's8'):
                position_count.labels(system=sys_name).set(counts.get(sys_name, 0))
    except Exception:
        pass

def collect_wallet():
    try:
        from shared.binance_api import load_config, fapi_get
        cfg = load_config()
        acc = fapi_get('/fapi/v2/account')
        if isinstance(acc, dict):
            bal = float(acc.get('totalWalletBalance', 0))
            wallet_balance.set(bal)
            total_mr = sum(abs(float(p.get('positionInitialMargin', 0)))
                          for p in acc.get('positions', []))
            margin_used.set(total_mr)
            ratio = total_mr / bal if bal > 0 else 0
            budget_ratio.set(ratio)
            if ratio > 0.75:
                tg_alert('budget_high', f'预算使用超75%（{ratio*100:.0f}%）')
    except Exception:
        pass

def collect_daily_pnl():
    today = time.strftime('%Y-%m-%d')
    for sys_name in ('s6', 's8'):
        sql = f"SELECT coalesce(sum(pnl_usdt),0) FROM default.trade_history WHERE source='{sys_name}' AND toDate(trade_time)='{today}'"
        pnl = ch_scalar(sql)
        try:
            daily_pnl.labels(system=sys_name).set(float(pnl) if pnl else 0.0)
        except:
            pass

def collect_loss_breach():
    for sys_name in ('s6', 's8'):
        breach = 0
        try:
            r = redis_get(f'daily_loss:{sys_name}')
            if r and r.get('stopped'):
                breach = 1
                tg_alert(f'loss_breach_{sys_name}', f'[{sys_name.upper()}] 已达日亏熔断')
        except:
            pass
        daily_loss_breach.labels(system=sys_name).set(breach)

def collect_pause():
    try:
        paused = redis_get('system:pause_new_pos')
        system_paused.set(1 if paused else 0)
    except Exception:
        system_paused.set(0)

def collect_algo_orders():
    try:
        from shared.binance_api import fapi_get
        orders = fapi_get('/fapi/v1/openAlgoOrders')
        algo_orders_count.set(len(orders) if isinstance(orders, list) else -1)
    except Exception:
        algo_orders_count.set(-1)

# ════════════════════════════════════════════════════════
def collect():
    collect_system_health()
    collect_heartbeat_age()
    collect_errors()
    collect_s3_signals()     # replaces old collect_s2_signals
    # collect_s6b_state()  # s6b merged into S6      # new
    collect_positions()
    collect_wallet()
    collect_daily_pnl()
    collect_loss_breach()
    collect_pause()
    collect_algo_orders()

if __name__ == '__main__':
    PORT = 8000
    start_http_server(PORT)
    print(f"[metrics_collector] listening on :{PORT} (S3/S6B updated)")
    while True:
        try:
            collect()
        except Exception as e:
            print(f"[collect error] {e}")
        time.sleep(15)
