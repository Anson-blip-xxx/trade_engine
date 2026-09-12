"""P5-05：Phase 5 architecture —— s3 package（core/state/detector/ports）纯度审计。"""
import ast
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

S3_MODULES = ['s3', 's3.core', 's3.state', 's3.detector', 's3.ports']


def module_source(name):
    root = REPO / name.replace('.', '/')
    cand = root / '__init__.py' if root.is_dir() else None
    if cand and cand.exists():
        return cand.read_text()
    return (REPO / (name.replace('.', '/') + '.py')).read_text()


def imported_roots(name):
    tree = ast.parse(module_source(name))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    return roots


FORBIDDEN = {'redis', 'requests', 'websocket', 'strategies', 'shared',
             'execution', 'risk', 'decision', 'binance', 'psycopg',
             'telegram', 'scripts', 'services', 'threading', 'time'}


def test_every_s3_module_import_clean():
    for mod in S3_MODULES:
        roots = imported_roots(mod)
        assert not (roots & FORBIDDEN), f'{mod} -> {roots & FORBIDDEN}'


def test_core_and_detector_and_state_purity():
    for mod in ('s3.core', 's3.detector', 's3.state'):
        assert imported_roots(mod) <= {'__future__', 'typing'}


def test_ports_only_injective_protocol():
    assert imported_roots('s3.ports') <= {'__future__', 'typing'}


def test_s3_package_import_smoke():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            + ''.join(f'import {m}\n' for m in S3_MODULES) +
            "after = threading.active_count()\n"
            "bad = [m for m in ('redis', 'requests', 'psycopg', 'websocket',\n"
            "                   'shared.redis_store', 'shared.postgres_client',\n"
            "                   'binance', 'strategies.s3_orderflow')\n"
            "        if m in sys.modules]\n"
            "print(before, after, bad)\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=120)
    assert out.returncode == 0, out.stderr       # no circular import
    before, after, bad = out.stdout.strip().split(maxsplit=2)
    assert before == after and bad == '[]'


def test_no_circular_import_order_variants():
    for order in (['s3.ports', 's3.core', 's3.state', 's3.detector'],
                  ['s3.detector', 's3.ports', 's3.state', 's3.core'],
                  ['s3.state', 's3.detector', 's3.core', 's3.ports']):
        code = '\n'.join(f'import {m}' for m in order)
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(REPO), timeout=120)
        assert out.returncode == 0, out.stderr
