"""Optional PostgreSQL ledger for position-level trade accounting.

The trading loop remains functional when PostgreSQL is not configured. Once
POSTGRES_DSN is supplied and psycopg is installed, PostgreSQL becomes the
transactional source of truth while ClickHouse remains an analytics sink.
"""
import json
import os
from contextlib import contextmanager
from pathlib import Path


_CONFIG_FILE = Path(__file__).resolve().parent.parent / 'config/binance.env'


def _config_value(name: str) -> str:
    try:
        for line in _CONFIG_FILE.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                if key.strip() == name:
                    return value.strip()
    except Exception:
        pass
    return ''


def _dsn() -> str:
    return (os.environ.get('POSTGRES_DSN') or _config_value('POSTGRES_DSN')).strip()


def enabled() -> bool:
    flag = os.environ.get('POSTGRES_ENABLED') or _config_value('POSTGRES_ENABLED') or '1'
    return bool(_dsn()) and flag.lower() not in ('0', 'false', 'no')


@contextmanager
def _connection():
    if not enabled():
        raise RuntimeError('PostgreSQL is not configured')
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError('psycopg is not installed') from exc
    conn = psycopg.connect(_dsn(), connect_timeout=3)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_trade_episode(data: dict) -> bool:
    """Idempotently write one complete position-level trade."""
    if not enabled():
        return False
    sql = """
    INSERT INTO trade_episodes (
        position_id, symbol, system_name, side, entry_price, exit_price,
        qty, leverage, pnl_pct, pnl_usdt, duration_min, result, exit_reason,
        event_type, strength, margin_mode, sl_price, ghost_cleanup,
        opened_at, closed_at, metadata
    ) VALUES (
        %(position_id)s, %(symbol)s, %(system_name)s, %(side)s, %(entry_price)s,
        %(exit_price)s, %(qty)s, %(leverage)s, %(pnl_pct)s, %(pnl_usdt)s,
        %(duration_min)s, %(result)s, %(exit_reason)s, %(event_type)s,
        %(strength)s, %(margin_mode)s, %(sl_price)s, %(ghost_cleanup)s,
        to_timestamp(%(open_time)s), now(), %(metadata)s::jsonb
    )
    ON CONFLICT (position_id) DO UPDATE SET
        exit_price = EXCLUDED.exit_price,
        qty = EXCLUDED.qty,
        pnl_pct = EXCLUDED.pnl_pct,
        pnl_usdt = EXCLUDED.pnl_usdt,
        duration_min = EXCLUDED.duration_min,
        result = EXCLUDED.result,
        exit_reason = EXCLUDED.exit_reason,
        ghost_cleanup = EXCLUDED.ghost_cleanup,
        closed_at = EXCLUDED.closed_at,
        metadata = EXCLUDED.metadata
    """
    params = dict(data)
    params['metadata'] = json.dumps(params.get('metadata', {}), default=str)
    try:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
        return True
    except Exception:
        return False


def record_trade_event(data: dict) -> bool:
    """Write one idempotent order/fill event to the ledger."""
    if not enabled():
        return False
    sql = """
    INSERT INTO trade_events (
        event_id, position_id, event_type, order_id, fill_id, price, qty,
        realized_pnl, occurred_at, payload
    ) VALUES (
        %(event_id)s, %(position_id)s, %(event_type)s, %(order_id)s,
        %(fill_id)s, %(price)s, %(qty)s, %(realized_pnl)s, now(), %(payload)s::jsonb
    )
    ON CONFLICT (event_id) DO NOTHING
    """
    params = dict(data)
    params['payload'] = json.dumps(params.get('payload', {}), default=str)
    try:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
        return True
    except Exception:
        return False
