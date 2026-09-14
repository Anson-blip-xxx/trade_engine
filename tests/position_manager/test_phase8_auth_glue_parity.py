"""P8-04：auth glue 迁移 parity（key source / call-time / failure / header）。"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

import shared.position_manager as pm
from position_market.auth import (
    load_api_keys, build_api_header, has_credentials)


def _env_file(tmp_path: Path, content: str) -> Path:
    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir(exist_ok=True)
    p = cfg_dir / 'binance.env'
    p.write_text(content)
    return p


class TestLoadApiKeysParity:
    def test_mainnet_pair(self, tmp_path):
        path = _env_file(tmp_path, 'BINANCE_API_KEY=K1\n'
                                    'BINANCE_API_SECRET=S1\n')
        assert load_api_keys(path) == ('K1', 'S1')

    def test_testnet_flag_true_ignores_mainnet_keys(self, tmp_path):
        path = _env_file(tmp_path, 'BINANCE_TESTNET=true\n'
                                   'BINANCE_API_KEY=IGNORED\n'
                                   'BINANCE_API_SECRET=IGNORED2\n')
        assert load_api_keys(path) == (None, None)

    def test_testnet_segment(self, tmp_path):
        path = _env_file(tmp_path, 'BINANCE_TESTNET=true\n'
                                   'BINANCE_TESTNET_API_KEY=KT\n'
                                   'BINANCE_TESTNET_API_SECRET=ST\n')
        assert load_api_keys(path) == ('KT', 'ST')

    def test_comment_and_malformed_lines_skipped(self, tmp_path):
        path = _env_file(tmp_path, '# comment\n'
                                   'not-a-pair\n'
                                   'BINANCE_API_KEY = K3\n'
                                   'noise\n')
        assert load_api_keys(path) == ('K3', None)

    def test_missing_file(self, tmp_path):
        assert load_api_keys(tmp_path / 'no.env') == (None, None)

    def test_read_text_exception_propagates(self, tmp_path):
        class BoomPath:
            def exists(self):
                return True

            def read_text(self):
                raise RuntimeError('fs')
        with pytest.raises(RuntimeError):
            load_api_keys(BoomPath())


class TestOtherHelpers:
    def test_header_exact(self):
        h = build_api_header('fake-key')
        assert list(h) == ['X-MBX-APIKEY']

    def test_presence_check_semantics(self):
        assert has_credentials('K', 'S') is True
        assert has_credentials('', 'S') is False
        assert has_credentials(None, 'S') is False
        assert has_credentials('K', None) is False
        assert has_credentials(' ', 'S') is True   # HEAD `not ' ' is False


class TestPMEnsureApikeyParity:
    def test_lazy_globals_persisted(self, tmp_path, monkeypatch):
        """HEAD 语义：`is None` call-time 判别 → 解析后 globals 持久缓存，
        后续调用不再读 env。test：spy `load_api_keys` 调用次数。"""
        _env_file(tmp_path, 'BINANCE_API_KEY=PK\n'
                            'BINANCE_API_SECRET=PS\n')
        monkeypatch.setattr(pm, '_BASE', tmp_path)
        pm._API_KEY = None
        pm._API_SECRET = None
        calls = []
        orig_load = pm._auth_helper.load_api_keys

        def spy_load(path):
            calls.append(1)
            return orig_load(path)
        monkeypatch.setattr(pm._auth_helper, 'load_api_keys', spy_load)
        pm._ensure_apikey()
        pm._ensure_apikey()            # 第二次 is-None guard → 不再解析
        assert pm._API_KEY == 'PK' and pm._API_SECRET == 'PS'
        assert len(calls) == 1
        monkeypatch.setattr(pm, '_API_KEY', None)
        monkeypatch.setattr(pm, '_API_SECRET', None)

    def test_env_missing_keeps_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, '_BASE', tmp_path)
        monkeypatch.setattr(pm, '_API_KEY', None)
        monkeypatch.setattr(pm, '_API_SECRET', None)
        pm._ensure_apikey()
        assert pm._API_KEY is None and pm._API_SECRET is None
        monkeypatch.setattr(pm, '_API_KEY', None, raising=False)
        monkeypatch.setattr(pm, '_API_SECRET', None, raising=False)

    def test_partial_keys(self, tmp_path, monkeypatch):
        path = _env_file(tmp_path, 'BINANCE_API_KEY=ONLYK\n')
        monkeypatch.setattr(pm, '_BASE', tmp_path)
        monkeypatch.setattr(pm, '_API_KEY', None)
        monkeypatch.setattr(pm, '_API_SECRET', None)
        pm._ensure_apikey()
        assert pm._API_KEY == 'ONLYK' and pm._API_SECRET is None
        monkeypatch.setattr(pm, '_API_KEY', None)
        monkeypatch.setattr(pm, '_API_SECRET', None)

    # 清理：无 transport 调用
    def test_no_transport_call(self, tmp_path, monkeypatch):
        path = _env_file(tmp_path, 'BINANCE_API_KEY=X\n'
                                   'BINANCE_API_SECRET=Y\n')
        monkeypatch.setattr(pm, '_BASE', tmp_path)
        monkeypatch.setattr(pm, '_API_KEY', None)
        monkeypatch.setattr(pm, '_API_SECRET', None)
        def boom(*a, **kw):
            raise AssertionError('transport 调用被触发')
        monkeypatch.setattr(pm.requests, 'post', boom)
        monkeypatch.setattr(pm.requests, 'get', boom)
        pm._ensure_apikey()
        monkeypatch.setattr(pm, '_API_KEY', None)
        monkeypatch.setattr(pm, '_API_SECRET', None)


class TestCleanImport:
    def test_subprocess_clean_import(self):
        res = subprocess.run(
            [sys.executable, '-c',
             "import os; os.environ['PM_NO_WS']='1'; "
             "import position_market.auth;"
             "print(position_market.auth.build_api_header.__name__)"],
            capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == 'build_api_header'

    def test_import_no_thread_no_io(self):
        before = set(threading.enumerate())
        import position_market.auth
        assert before == set(threading.enumerate())
        src = open(position_market.auth.__file__).read()
        assert 'requests' not in src
