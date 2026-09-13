"""P7-02：PositionStateService 架构纯度 guard。

- service 不 import PM / shared_executor / execution.service / strategies
- 无线程、无 import 副作用（subprocess sys.modules clean）
- 依赖方向：PM → service → port/callables → redis helper（无反向）
"""
import ast
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_service_imports_limited():
    src = (REPO / 'position_state' / 'service.py').read_text()
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    # stdlib + typing only（blocks from shared etc.）
    assert not (roots & {'redis', 'requests', 'psycopg', 'clickhouse',
                         'shared', 'strategies', 'services', 'binance',
                         'execution'}), roots


def test_service_subprocess_clean_import():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            "import position_state.service\n"
            "after = threading.active_count()\n"
            "bad = [m for m in ('redis', 'requests', 'psycopg',\n"
            "                   'shared.redis_store', 'binance',\n"
            "                   'shared.position_manager',\n"
            "                   'strategies.shared_executor')\n"
            "        if m in sys.modules]\n"
            "print(before, after, bad)\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=60)
    assert out.returncode == 0, out.stderr
    before, after, bad = out.stdout.strip().split(maxsplit=2)
    assert before == after and bad == '[]'


def test_pm_wiring_frozen():
    """PM → service 单向；service/module 无对 PM 引用。"""
    pm_src = (REPO / 'shared' / 'position_manager.py').read_text()
    assert 'ps_service.PositionStateService' in pm_src
    svc_src = (REPO / 'position_state' / 'service.py').read_text()
    assert 'position_manager' not in svc_src
    assert 'shared_executor' not in svc_src


def test_service_no_thread_spawn():
    thread_start = [l for l in (REPO / 'position_state' / 'service.py')
                    .read_text().splitlines()
                    if 'threading' in l or 'Thread' in l]
    assert thread_start == []
