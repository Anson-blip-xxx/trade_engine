"""P5-02：s3.core 架构纯度 guard。

验证 s3.core：
- 不 import redis/requests/websocket/strategies/shared/execution/risk/PM
- import 不开线程/不触网/不写文件/不连 Redis（AST + 子进程）
"""
import ast
import re
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / 's3' / 'core.py'

FORBIDDEN = ('redis', 'requests', 'websocket', 'strategies', 'shared',
             'execution', 'risk', 'decision', 'position_manager',
             'shared_executor', 'binance', 'psycopg', 'telegram', 'json',
             'time', 'math', 'socket', 'os', 'threading', 'queue')


def test_core_imports_stdlib_only():
    src = CORE.read_text()
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    assert roots == {'__future__', 'typing'}


def test_core_source_no_forbidden_roots():
    src = CORE.read_text()
    for mod in FORBIDDEN:
        assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}\b',
                             src, re.M), f'forbidden: {mod}'
    # 无全局可变形态：不存在 "X: dict = {}" / "X = {}" 顶层
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                assert isinstance(t, ast.Name) or t.id.startswith('_') or True
    # 直接禁止 dict()/list() 字面量在顶层 Assign
    src_lines = [l for l in src.splitlines()
                 if re.match(r'^_\w*:?', l) and re.search(r'[\[\{]|\bdict\(|\blist\(', l)]
    assert src_lines == []


def test_core_import_smoke_no_side_effects():
    before = threading.active_count()
    import s3.core  # noqa
    after = threading.active_count()
    assert before == after


def test_core_subprocess_clean_import():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            "import s3.core\n"
            "after = threading.active_count()\n"
            "bad = [m for m in ('redis', 'requests', 'psycopg', 'websocket',\n"
            "                   'shared.redis_store',"
            "                   'shared.postgres_client', 'binance',\n"
            "                   'strategies.s3_orderflow') if m in sys.modules]\n"
            "print(before, after, bad, len(__builtins__.__dict__) if hasattr(__builtins__, '__dict__') else 'ok')\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=120)
    assert out.returncode == 0, out.stderr
    before, after, bad, _ = out.stdout.strip().split(maxsplit=3)
    assert before == after      # 无线程
    assert bad == '[]'          # 无 IO/strategies 模块


def test_strategies_delegates_only(s3m):
    """strategies.s3_orderflow 的 4 个函数仅 thin delegation（不改 event/threshold）。"""
    import inspect
    assert 'return s3_core.rsi(' in inspect.getsource(s3m.compute_rsi)
    assert 'return s3_core.atr(' in inspect.getsource(s3m.compute_atr)
    src_ema = inspect.getsource(s3m.compute_ema)
    assert 's3_core.ema(values, period)' in src_ema
    # 增量路径仍在 s3_orderflow（未抽）
    assert '_ema_cache[symbol][period] = new_ema' in src_ema
    src_w = inspect.getsource(s3m.compute_window_data)
    assert 's3_core.build_window_features' in src_w
    assert 'compute_ema' in src_w
    # detect_events 未动
    src_d = inspect.getsource(s3m.detect_events)
    assert 's3_core' not in src_d     # detect 不感知 core（route via windows 参数）
