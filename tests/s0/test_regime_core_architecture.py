"""P6-02：s0.core 纯度架构 guard（stdlib only、无 IO、无时钟、无全局态）。"""
import ast
import re
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / 's0' / 'core.py'

FORBIDDEN = ('redis', 'requests', 'websocket', 'clickhouse', 'strategies',
             'shared', 'services', 'execution', 'risk', 'decision',
             'position_manager', 'shared_executor', 'binance', 'psycopg',
             'telegram', 'threading', 'queue', 'time', 'socket', 'os')

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


def test_core_no_module_mutable_state():
    src_lines = [l for l in CORE.read_text().splitlines()
                 if re.match(r'^_\w*:?=', l)]
    assert src_lines == []


def test_core_subprocess_clean_import():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            "import s0.core\n"
            "after = threading.active_count()\n"
            "bad = [m for m in ('redis', 'requests', 'psycopg', 'telegram',\n"
            "                   'shared.redis_store',\n"
            "                   'shared.postgres_client', 'binance',\n"
            "                   'services.s0.s0_market_guard')\n"
            "        if m in sys.modules]\n"
            "print(before, after, bad)\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=60)
    assert out.returncode == 0, out.stderr
    before, after, bad = out.stdout.strip().split(maxsplit=2)
    assert before == after and bad == '[]'


def test_legacy_wrapper_thin_only(s0_env):
    """compute_state 只保留采样 IO 与分类委托（决策表主体在 core）。"""
    import inspect
    import services.s0.s0_market_guard as g
    src = inspect.getsource(g.compute_state)
    assert 's0_core.classify_regime' in src
    assert 'sample_sentiment' in src
    assert '% 1800' in src and '% 60' in src     # wall-clock gate 留壳（S0-3）
    # 事件分类规则不残留壳内（决策表全在 core）
    assert 'bull_trend' not in src
    assert 'risk_off = (' not in src
