"""P6-03：s0 publisher 架构纯度 guard。

Port：stdlib-only；adapter 不依赖 classifier core；无循环依赖；
subprocess clean import；orchestration 只走 thin 写入。
"""
import ast
import re
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def module_source(name):
    root = REPO / name.replace('.', '/')
    cand = root / '__init__.py' if root.is_dir() else None
    if cand and cand.exists():
        return cand.read_text()
    return (REPO / (name.replace('.', '/') + '.py')).read_text()


def test_port_imports_stdlib_only():
    src = module_source('s0.ports')
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
    assert roots == {'__future__', 'typing'}


def test_adapter_no_classifier_no_core():
    """adapter 不 import classifier；core 不引用 publisher（避免 s0.core→publisher）。"""
    import re
    for mod in ('s0.adapters', 's0.core', 's0.ports'):
        roots = set()
        tree = ast.parse(module_source(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split('.')[0])
        assert not (roots & {'services', 's0_market_guard'}), mod
        if mod == 's0.core':
            assert 's0.ports' not in module_source(mod)
            assert 's0.adapters' not in module_source(mod)


def test_port_adapter_no_forbidden_roots():
    for mod in ('s0.ports', 's0.adapters'):
        src = module_source(mod)
        for modname in ('redis', 'requests', 'clickhouse', 'psycopg',
                        'strategies', 'shared', 'execution', 'services.s0',
                        'position_manager', 'shared_executor', 'telegram'):
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(modname)}\b',
                                  src, re.M), f'{mod} -> {modname}'


def test_subprocess_clean_import_no_cycle():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            "import s0.core\nimport s0.ports\nimport s0.adapters\n"
            "after = threading.active_count()\n"
            "bad = [m for m in ('redis', 'requests', 'psycopg',\n"
            "                   'shared.clickhouse_client',\n"
            "                   'shared.redis_store', 'binance',\n"
            "                   'services.s0.s0_market_guard')\n"
            "        if m in sys.modules]\n"
            "print(before, after, bad)\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=60)
    assert out.returncode == 0, out.stderr
    before, after, bad = out.stdout.strip().split(maxsplit=2)
    assert before == after and bad == '[]'


def test_write_state_thin_wiring_only(s0_env):
    import inspect
    import services.s0.s0_market_guard as g
    src = inspect.getsource(g.write_state)
    assert 'publish_state' in src
    assert 'market:s0' not in src          # redis 落地逻辑在 adapter
    assert 'market_state_log' not in src
    # classifier 未被触碰
    src_c = inspect.getsource(g.compute_state)
    assert 's0_core.classify_regime' in src_c
