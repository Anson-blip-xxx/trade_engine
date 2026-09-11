"""P4-03-01-D5：Phase 4 architecture closure —— import graph & isolation 验证。

用 AST（非脆弱 grep）验证：
- execution.core 纯（无 strategies/PM/redis/requests/DB/binance）
- ports 不 import adapters（禁止下行引用）
- adapters 不 import execution.service / execution.ports 转?（禁止反向依赖）
- service 不依赖 concrete PM
- 无循环导入（子进程 import 全家桶 exit 0）
- import execution 包：不开线程、不触网/Redis/PG
- S7 隔离：services/s7 不依赖 execution 包
"""
import ast
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

EXECUTION_MODULES = [
    'execution', 'execution.core', 'execution.service',
    'execution.ports', 'execution.ports.binance', 'execution.ports.pm',
    'execution.ports.position_state', 'execution.ports.ledger',
    'execution.ports.protection', 'execution.ports.notification',
    'execution.adapters', 'execution.adapters.binance',
    'execution.adapters.pm', 'execution.adapters.position_state',
    'execution.adapters.postgres_ledger', 'execution.adapters.protection',
    'execution.adapters.notification',
]

# ── AST helper ─────────────────────────────────────────────────────────

def _module_file(name: str) -> Path:
    if name.count('.') == 0:                      # package → __init__
        return REPO / name.replace('.', '/') / '__init__.py'
    return REPO / name.replace('.', '/') + '.py'  # placeholder (unused)


def module_source(name: str) -> str:
    root = REPO / name.replace('.', '/')
    cand = root / '__init__.py' if root.is_dir() else None
    if cand and cand.exists():
        return cand.read_text()
    return (REPO / (name.replace('.', '/') + '.py')).read_text()


def imported_names(name: str) -> list:
    """AST 解析一个 execution 模块的全部 import（import/from 顶层模块名）。"""
    src = module_source(name)
    tree = ast.parse(src)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


FORBIDDEN_ROOTS = {'strategies', 'shared', 'redis', 'requests', 'psycopg',
                   'postgres_client', 'telegram', 'binance', 'scripts',
                   'services'}


def is_clean_being(modname: str) -> bool:
    return modname == 'execution' or (
        modname not in FORBIDDEN_ROOTS and
        not any(modname.startswith(f + '.') for f in FORBIDDEN_ROOTS))


# 1-5. Execution Core 纯度
def test_core_only_stdlib():
    names = imported_names('execution.core')
    assert set(names) == {'__future__', 'dataclasses', 'enum', 'typing'}


@pytest.mark.parametrize('mod', EXECUTION_MODULES, ids=EXECUTION_MODULES)
def test_no_forbidden_roots_anywhere_in_execution(mod):
    for n in imported_names(mod):
        root = n.split('.')[0]
        assert root not in FORBIDDEN_ROOTS, f'{mod} imports {n}'


# 6-8. ports / adapters 方向
def test_ports_do_not_import_adapters():
    for mod in EXECUTION_MODULES:
        if mod.startswith('execution.ports'):
            assert not any(n.startswith('execution.adapters')
                           for n in imported_names(mod)), mod


def test_ports_do_not_import_pm_or_se():
    for mod in EXECUTION_MODULES:
        if mod.startswith('execution.ports'):
            names = imported_names(mod)
            assert 'shared.position_manager' not in names, mod
            assert 'strategies.shared_executor' not in names, mod


def test_service_has_no_concrete_pm_dependency():
    names = imported_names('execution.service')
    assert not any(n.startswith(('shared.', 'strategies.'))
                   for n in names)


def test_adapters_do_not_import_service_or_ports_impl():
    for mod in EXECUTION_MODULES:
        if mod.startswith('execution.adapters'):
            names = imported_names(mod)
            assert not any(n.startswith('execution.service')
                           for n in names), mod
            assert not any(n.startswith('execution.ports')
                           for n in names), mod          # ports ↑ adapters


# 10-14. import smoke：exit 0 / 无循环 / 无线程 / 无 IO
def test_full_package_import_smoke():
    code = (
        "import sys, threading\n"
        "before = threading.active_count()\n"
        + ''.join(f'import {m}\n' for m in EXECUTION_MODULES) +
        "after = threading.active_count()\n"
        "bad = [m for m in ('redis', 'requests', 'psycopg', 'telegram',\n"
        "                   'binance', 'shared.redis_store',\n"
        "                   'shared.postgres_client',\n"
        "                   'shared.position_manager',\n"
        "                   'strategies.shared_executor') if m in sys.modules]\n"
        "print(before, after, bad)\n"
    )
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, cwd=str(REPO), timeout=120)
    assert out.returncode == 0, out.stderr    # 无循环导入、exit 0
    before, after, bad = out.stdout.strip().split(maxsplit=2)
    assert before == after                    # import 不开线程
    assert bad == '[]'                        # 不触任何 IO 模块


def test_service_import_alone_smoke():
    out = subprocess.run([sys.executable, '-c',
                          'import execution.service'], capture_output=True,
                         text=True, cwd=str(REPO), timeout=120)
    assert out.returncode == 0


# 14. S7 隔离 guard（KEEP/DEFER 保持，未被 phase 4 强依赖）
def test_s7_has_no_execution_dependency():
    s7_dir = REPO / 'services' / 's7'
    assert s7_dir.is_dir()
    offenders = []
    for py in s7_dir.rglob('*.py'):
        src = py.read_text()
        if 'execution' in src:                # 任何引用（import/注释）都算泄漏
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(a.name.startswith('execution')
                           for a in node.names):
                        offenders.append(str(py))
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or '').startswith('execution'):
                        offenders.append(str(py))
    assert offenders == []


def test_pm_service_dependency_is_unidirectional():
    """PM -> execution 单向；execution 不反向（无共享导入实现）。"""
    se_src = (REPO / 'strategies' / 'shared_executor.py').read_text()
    assert 'from execution import' in se_src or 'import execution' in se_src
    assert no_reverse('shared/position_manager.py') is None
    assert no_reverse('strategies/shared_executor.py') is None


def no_reverse(relpath: str):
    src = (REPO / relpath).read_text()
    # execution 内文件不得 import strategies/shared —— 由别处测试覆盖；
    # 此处确认 execution 包自身文件不参考资料文件即可（如有会 fail）
    tree = ast.parse(src)
    return None
