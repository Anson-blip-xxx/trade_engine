"""P7-02/P7-03B：architecture guards（service→port 单向 + service isolation）。"""
import ast
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

FORBIDDEN_IN_SERVICE = {'redis', 'requests', 'psycopg', 'clickhouse',
                        'shared', 'services', 'strategies', 'execution',
                        'binance', 'telegram'}


def test_position_state_core_imports_clean():
    src = (REPO / 'position_state' / 'service.py').read_text()
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    assert not (roots & FORBIDDEN_IN_SERVICE), roots


def test_ledger_service_imports_limited():
    src = (REPO / 'position_ledger' / 'service.py').read_text()
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    assert not (roots & (FORBIDDEN_IN_SERVICE - {'shared'})), roots


def test_service_subprocess_clean_import():
    for mod in ('position_state.service', 'position_ledger.service'):
        code = ("import sys, threading\n"
                "before = threading.active_count()\n"
                f"import {mod}\n"
                "after = threading.active_count()\n"
                "bad = [m for m in ('redis', 'requests', 'psycopg',\n"
                "                   'shared.clickhouse_client',\n"
                "                   'shared.redis_store', 'binance',\n"
                "                   'shared.position_manager',\n"
                "                   'strategies.shared_executor')\n"
                "        if m in sys.modules]\n"
                "print(before, after, bad)\n")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(REPO), timeout=60)
        assert out.returncode == 0, out.stderr
        before, after, bad = out.stdout.strip().split(maxsplit=2)
        assert before == after and bad == '[]', mod


def test_no_reverse_dependency():
    for mod in ('position_state.service', 'position_ledger.service'):
        src_path = REPO / (mod.replace('.', '/') + '.py')
        src = src_path.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith('shared.position_manager'), mod
                    assert not a.name.startswith('strategies.shared_executor'), mod
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith('shared.position_manager'), mod
                assert not node.module.startswith('strategies.shared_executor'), mod


def test_service_no_thread_spawns():
    for mod in ('position_state/service.py', 'position_ledger/service.py'):
        src = (REPO / mod).read_text()
        assert 'threading' not in src and 'Thread' not in src
