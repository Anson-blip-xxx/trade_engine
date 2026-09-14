"""P8-04：auth glue 架构 guard（leaf / transport/signing 不迁 / 无 cache）。"""
import ast
import subprocess
import sys
import threading

import pytest

import shared.position_manager as pm
import position_market.auth as au


class TestLeafDependency:
    def test_imports_only_typing(self):
        tree = ast.parse(open(au.__file__).read())
        mods = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                mods.add(node.module or '')
        assert mods <= {'__future__', 'typing', 'pathlib'}   # leaf + stdlib

    def test_no_transport_no_signing_import(self):
        src = open(au.__file__).read()
        for name in ('requests', 'hmac', 'hashlib', 'redis',
                     'threading', 'position_manager', 'shared_executor',
                     'execution', 'monitoring'):
            assert name not in src, name


class TestNoCoupling:
    def test_no_signing_no_timestamp_no_recvwindow(self):
        src = open(au.__file__).read()
        for token in ('timestamp', 'recvWindow', 'hmac', 'signature'):
            assert token not in src, token

    def test_no_transport_call_tokens(self):
        src = open(au.__file__).read()
        for token in ('requests.get', 'requests.post', 'FAPI'):
            assert token not in src, token


class TestSigningUnchanged:
    def test_pm_signing_source_tokens_in_light_fapi(self):
        src = open(pm.__file__).read()
        # signing 綁 transport 的原位（_light_fapi_post 保留原 body）
        assert "hmac.new(_API_SECRET.encode('utf-8'), query.encode('utf-8')" in src
        assert "params['signature'] = sig" in src

    def test_light_fapi_source_unchanged_clean(self, monkeypatch):
        res = subprocess.run(['git', 'diff', 'de18975', 'HEAD', '--',
                              'shared/position_manager.py'],
                             capture_output=True, text=True)
        # diff 中不得出现 _light_fapi 主体签名替换
        leaked = [l for l in res.stdout.splitlines()
                  if '_light_fapi' in l and l.startswith(('+', '-'))]
        assert leaked == []            # C1 untouched


class TestNoCredentialCache:
    def test_helper_stateless(self):
        tree = ast.parse(open(au.__file__).read())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        assert t.id != 'KEY' and t.id != 'SECRET'


class TestCleanImport:
    def test_import_no_thread_spawn(self):
        before = set(threading.enumerate())
        import position_market.auth
        assert before == set(threading.enumerate())

    def test_pm_auth_helper_alias(self):
        assert pm._auth_helper is au

    def test_no_secret_leakage_in_tests(self):
        """测试不出现真实 key（fake 值 only）。"""
        src = open('tests/position_manager/test_phase8_auth_glue_parity.py').read()
        assert 'AAH0rrQrL' not in src        # TG token 样本（防泄漏）
