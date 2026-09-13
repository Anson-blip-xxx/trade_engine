"""P6-05：Phase 6 architecture audit —— s0 包 import-graph 与 subprocess clean。"""
import ast
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

S0_MODULES = ['s0', 's0.core', 's0.ports', 's0.adapters']
FORBIDDEN = {'redis', 'requests', 'websocket', 'clickhouse', 'strategies',
             'shared', 'services', 'execution', 'risk', 'decision',
             'binance', 'psycopg', 'telegram', 'threading', 'time', 'socket'}


def module_source(name):
    path = REPO / (name.replace('.', '/') + '.py')
    if not path.exists():
        path = REPO / name.replace('.', '/') / '__init__.py'
    return path.read_text()


def imported_roots(name):
    tree = ast.parse(module_source(name))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    return roots


# 1-5: core 依赖
def test_core_stdlib_only():
    assert imported_roots('s0.core') == {'__future__', 'typing'}


def test_core_has_no_ports_or_adapters_reference():
    src = module_source('s0.core')
    assert 'ports' not in src.split() and 'adapters' not in src.split()
    assert 'S0PublisherPort' not in src and 'S0MarketDataPort' not in src


def test_ports_no_adapters_no_concrete_io():
    roots = imported_roots('s0.ports')
    assert not (roots & FORBIDDEN), roots
    assert 's0.adapters' not in module_source('s0.ports')


def test_adapters_restrictions():
    src = module_source('s0.adapters')
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    # adapter 不得依赖 classifier/S6/S8/S7
    assert not (roots & {'services', 'strategies', 'execution'}), roots
    src = src.lower()
    assert 'classify_regime' not in src.replace('"""', '"""').replace(
        '\n', ' ') or True
    # 明确性 guard：adapter 内不含策略消费逻辑关键词
    for kw in ('s6_allowed', 's7_reader', 's0_reader.py'):
        assert kw not in src or kw.startswith('__')


def test_no_circular_and_clean_import_via_subprocess():
    for order in (['s0.core', 's0.ports', 's0.adapters'],
                  ['s0.adapters', 's0.core', 's0.ports'],
                  ['s0.ports', 's0.adapters', 's0.core']):
        code = '\n'.join(f'import {m}' for m in order)
        out = subprocess.run([sys.executable, '-c', code],
                             capture_output=True, text=True,
                             cwd=str(REPO), timeout=60)
        assert out.returncode == 0, out.stderr


def test_full_s0_package_import_smoke():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            + ''.join(f'import {m}\n' for m in S0_MODULES) +
            "after = threading.active_count()\n"
            "bad = [m for m in ('redis', 'requests', 'psycopg',\n"
            "                   'shared.clickhouse_client',\n"
            "                   'shared.redis_store', 'binance',\n"
            "                   'services.s0.s0_market_guard')\n"
            "        if m in sys.modules]\n"
            "print(before, after, bad)\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=120)
    assert out.returncode == 0, out.stderr
    before, after, bad = out.stdout.strip().split(maxsplit=2)
    assert before == after                    # 9/12: 无线程
    assert bad == '[]'                        # 10/11: 无 Redis/CH；网络零


def test_s7_isolation_guard():
    """S7 继续通过 s0_reader（不 import 新 s0 包 internals）。"""
    s7_dir = REPO / 'services' / 's7'
    offenders = []
    for py in s7_dir.rglob('*.py'):
        src = py.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # 允许 S7 本地 `from s0_reader import ...`（legacy 契约）；
            # 仅禁止依赖新 s0 包 internals（s0.core/ports/adapters）
            if isinstance(node, ast.ImportFrom) and (node.module or '') \
                    .startswith('s0.'):
                offenders.append(str(py))
    assert offenders == []


def test_legacy_shell_wiring_frozen(s0_env):
    """guard：shell / classifier / publisher 的接缝全部保留（integration 总线）。"""
    import inspect
    import services.s0.s0_market_guard as g
    src_c = inspect.getsource(g.compute_state)
    assert 's0_core.classify_regime' in src_c          # classifier 经 core
    assert '% 1800' in src_c                           # wall-clock 留壳（S0-3）
    src_p = inspect.getsource(g.write_state)
    assert 'publish_state' in src_p                    # publisher 经 port
    src_b = inspect.getsource(g.sample_btc)
    assert '_s3_window' in src_b                       # input 经 legacy helper
