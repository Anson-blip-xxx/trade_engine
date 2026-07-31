"""
ClickHouse 客户端 — 基于 clickhouse-connect SDK
统一管理连接，替代分散的 subprocess clickhouse-client 调用。
"""
import clickhouse_connect
from pathlib import Path

_CLIENT = None

_CONFIG_FILE = Path(__file__).resolve().parent.parent / 'config/binance.env'

def _cfg():
    env = {}
    try:
        for line in _CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def get_client():
    global _CLIENT
    if _CLIENT is None:
        env = _cfg()
        _CLIENT = clickhouse_connect.get_client(
            host=env.get('CLICKHOUSE_HOST', 'localhost'),
            port=int(env.get('CLICKHOUSE_PORT', '8123')),
            username=env.get('CLICKHOUSE_USER', 'admin'),
            password=env.get('CLICKHOUSE_PASSWORD', ''),
            database='default',
        )
    return _CLIENT


def query(sql: str) -> list[list]:
    """执行查询，返回结果行列表。"""
    try:
        r = get_client().query(sql)
        return r.result_rows if r and r.result_rows else []
    except Exception:
        return []


def query_column(sql: str) -> list:
    """执行查询，返回第一列列表。"""
    try:
        return list(get_client().query_column(sql) or [])
    except Exception:
        return []


def insert(table: str, data: str):
    """以 JSONEachRow 格式插入。data 是 JSON 字符串。"""
    try:
        get_client().command(f'INSERT INTO {table} FORMAT JSONEachRow {data}')
    except Exception:
        raise


def insert_rows(table: str, rows: list[list], column_names: list[str]):
    """批量插入。"""
    try:
        get_client().insert(table, rows, column_names=column_names)
    except Exception as e:
        raise
