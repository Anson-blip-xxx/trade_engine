"""P6-04：s0 input boundary 纯度 guard + 拆层 wiring 冻结。"""
import ast
import re
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_input_adapters_no_classifier_no_publisher():
    """依赖方向：orchestration → input port → adapter；core/publisher 无反向。"""
    for mod in ('s0.ports', 's0.adapters', 's0.core'):
        src_path = REPO / (mod.replace('.', '/') + '.py')
        if not src_path.exists():
            src_path = REPO / mod.replace('.', '/') / '__init__.py'
        src = src_path.read_text()
        tree = ast.parse(src)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split('.')[0])
        if mod == 's0.core':
            assert roots == {'__future__', 'typing'}
        else:
            assert not (roots & {'redis', 'requests', 'clickhouse', 'shared',
                                 'services', 'strategies', 'execution'}), mod
        # core/publisher 不反向依赖 input
        if mod == 's0.core':
            assert 'BreadthPool' not in src and 'S0MarketDataPort' not in src
        if mod == 's0.adapters':
            pass


def test_sample_btc_uses_helper_not_port_directly(s0_env):
    """sample_btc 仍调 `_s3_window`（legacy 入口）——port 经 factory 晚绑定。"""
    import inspect
    import services.s0.s0_market_guard as g
    src = inspect.getsource(g.sample_btc)
    assert '_s3_window' in src
    assert 's0_adapters' not in src             # sample_btc 不直接触 adapter
    src_w = inspect.getsource(g._s3_window)
    assert 'read_s3_window' in src_w            # helper 经 adapter


def test_subprocess_clean_import():
    code = ("import sys, threading\n"
            "before = threading.active_count()\n"
            "import s0.ports\nimport s0.adapters\n"
            "after = threading.active_count()\n"
            "bad = [m for m in ('redis', 'requests', 'psycopg',\n"
            "                   'shared.redis_store', 'binance',\n"
            "                   'services.s0.s0_market_guard')\n"
            "        if m in sys.modules]\n"
            "print(before, after, bad)\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=60)
    assert out.returncode == 0, out.stderr
    before, after, bad = out.stdout.strip().split(maxsplit=2)
    assert before == after and bad == '[]'


def test_s0_gaurd_wiring_frozen(s0_env):
    """冻结 wiring ~= ports 晚绑定 factory + 全局 wrap（重置后 clear）。"""
    import inspect
    import services.s0.s0_market_guard as g
    src = inspect.getsource(g)
    assert '_s0_market_data' in src and '_s0_breadth_pool' in src
    # compute_state/publish 零 diff 事实（guard 由 test 文件独立）
    src_c = inspect.getsource(g.compute_state)
    assert 's0_core.classify_regime' in src_c
    src_p = inspect.getsource(g.write_state)
    assert 'publish_state' in src_p
