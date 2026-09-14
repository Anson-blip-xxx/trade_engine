"""P7-04B：Protection Service 架构纯度 guard（service isolation/clean import）。"""
import ast
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_service_imports_limited():
    src = (REPO / 'position_protection' / 'service.py').read_text()
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    # 只.stdlib：typing + threading（threading 用于 typing 提示，不是 spawn）
    assert not (roots & {'redis', 'requests', 'psycopg', 'clickhouse',
                         'shared', 'services', 'strategies', 'execution',
                         'binance', 'telegram'}), roots


def test_service_subprocess_clean_import():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            "import position_protection.service\n"
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
    assert before == after and bad == '[]'   # import 无副作用，无 IO，无线程


def test_no_reverse_dependency():
    for mod in ('position_protection.service',):
        src_path = REPO / (mod.replace('.', '/') + '.py')
        src = src_path.read_text()
        assert 'shared.position_manager' not in src
        assert 'shared_executor' not in src
        assert 'ExecutionService' not in src
        assert 'PositionStateService' not in src
        assert 'LedgerService' not in src


def test_service_no_thread_spawns():
    src = (REPO / 'position_protection' / 'service.py').read_text()
    lines = [l for l in src.splitlines()
             if 'threading.Thread' in l or 'Thread(' in l]
    assert thread_start_filter(lines) == []


def thread_start_filter(lines):
    thread_lines = [l for l in lines if 'Thread(' in l and 'import' not in l]
    assert thread_lines == []
    return thread_lines


def test_service_method_surface_matches_port():
    import inspect
    from position_protection.service import ProtectionService
    from execution.ports.protection import ProtectionPort
    svc_methods = sorted(name for name, _ in inspect.getmembers(
        ProtectionService, predicate=inspect.isfunction)
        if not name.startswith('_'))
    port_methods = sorted(name for name, _ in inspect.getmembers(
        ProtectionPort, predicate=inspect.isfunction)
        if not name.startswith('_'))
    assert svc_methods == port_methods
