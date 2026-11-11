"""Real isolated child-process acceptance, with an entirely local fake SDK.

No production interpreter, shared configuration, credentials or network calls
are used. The fake wheel metadata exercises the actual SDK pin check.
"""

import json
import shutil
import subprocess
import sys
import venv

import pytest

from services import v2_jev_transport as transport
from v2_core import jev_advisory as core
from v2_core import symbols


@pytest.fixture(scope="module")
def isolated_sdk(tmp_path_factory):
    root = tmp_path_factory.mktemp("jev-isolated-sdk")
    interpreter = root / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(interpreter)
    site = (
        interpreter
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    (site / "typesafe_sdk.py").write_text(
        """
class TypeSafeAPIConnectionError(Exception): pass
class TypeSafeAPITimeoutError(Exception): pass
class TypeSafeAPIResponseValidationError(Exception): pass
class TypeSafeAPIError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__('PRIVATE_PROVIDER_BODY_NOT_ALLOWED')
def RetryPolicy(**kwargs):
    return kwargs
"""
    )
    (site / "jev_shared.py").write_text(
        """
import os
import socket
import sys
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typesafe_sdk import (
    TypeSafeAPIConnectionError, TypeSafeAPIError,
    TypeSafeAPITimeoutError, TypeSafeAPIResponseValidationError,
)

def no_network(*args, **kwargs):
    raise AssertionError('REAL_NETWORK_IS_FORBIDDEN')
socket.socket = no_network
socket.create_connection = no_network

class ConfigurationError(ValueError): pass

if os.environ.get('TYPESAFE_API_KEY') == 'synthetic-import-unavailable':
    raise ImportError('PRIVATE_IMPORT_BODY')

def create_client(**kwargs):
    assert kwargs == {'model': 'jev-1.13.0'}
    if os.environ.get('TYPESAFE_API_KEY') == 'synthetic-config-unavailable':
        raise ConfigurationError('PRIVATE_CONFIGURATION_BODY')
    assert sys.flags.isolated == 1
    assert os.environ['TYPESAFE_API_KEY'] == 'synthetic-isolated-sdk'
    assert 'PYTHONPATH' not in os.environ
    assert 'BINANCE_API_KEY' not in os.environ
    assert 'CREDENTIALS_DIRECTORY' not in os.environ
    assert not {'services', 'psycopg', 'redis'} & sys.modules.keys()
    assert sys.modules['v2_core'].__path__ == []
    assert importlib.util.find_spec('v2_core.evil') is None
    assert {k for k in sys.modules if k.startswith('v2_core.')} == {'v2_core.symbols'}
    contract = sys.modules['_v2_jev_contract']
    assert str(Path(contract.__file__).resolve().parents[1]) not in sys.path
    return Client()

class Client:
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def system_one(self, **request):
        assert request['retry'] == {'max_retries': 0, 'timeout': request['timeout']}
        symbol = request['state']['symbol']
        failures = {
            'ETHUSDT': TypeSafeAPITimeoutError('PRIVATE_TIMEOUT_BODY'),
            'SOLUSDT': TypeSafeAPIConnectionError('PRIVATE_CONNECTION_BODY'),
            'BNBUSDT': TypeSafeAPIError(429),
            'XRPUSDT': TypeSafeAPIResponseValidationError('PRIVATE_INVALID_BODY'),
            'ADAUSDT': ImportError('PRIVATE_AFTER_REQUEST_IMPORT_BODY'),
        }
        if symbol in failures:
            raise failures[symbol]
        result = {
            'model': 'jev-1.13.0',
            'answers': {name: {'type': 'noul', 'noul': '0.5'} for name in (
                'trend_supported', 'chase_risk', 'liquidity_fragile')},
            'usage': {'input_tokens': 123, 'output_tokens': 10},
        }
        return SimpleNamespace(model_dump=lambda **kwargs: result)
"""
    )
    metadata = site / "typesafe_sdk-0.7.5.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: typesafe-sdk\nVersion: 0.7.5\n"
    )
    release = root / "minimal-release"
    (release / "services").mkdir(parents=True)
    (release / "v2_core").mkdir()
    adapter = release / "services" / "v2_jev_transport.py"
    shutil.copyfile(transport.__file__, adapter)
    shutil.copyfile(core.__file__, release / "v2_core" / "jev_advisory.py")
    shutil.copyfile(symbols.__file__, release / "v2_core" / "symbols.py")
    (release / "v2_core" / "__init__.py").write_text(
        "raise AssertionError('PACKAGE_INIT_MUST_NOT_RUN')\n"
    )
    (release / "v2_core" / "evil.py").write_text(
        "raise AssertionError('SIBLING_MUST_NOT_BE_IMPORTABLE')\n"
    )
    untrusted = root / "untrusted-cwd"
    untrusted.mkdir()
    (untrusted / "jev_shared.py").write_text(
        "raise AssertionError('AMBIENT_MODULE_INJECTION')\n"
    )
    (untrusted / "json.py").write_text(
        "raise AssertionError('STDLIB_SHADOW_INJECTION')\n"
    )
    (untrusted / "v2_core").mkdir()
    (untrusted / "v2_core" / "jev_advisory.py").write_text(
        "raise AssertionError('AMBIENT_CORE_INJECTION')\n"
    )
    return interpreter / "bin" / "python", adapter, untrusted


def request(symbol="BTCUSDT"):
    return core.prepare_request(
        {
            "symbol": symbol,
            "observed_at_ms": 100000,
            "timeframe": "15m",
            "direction": "LONG",
            "price": "100",
            "atr_pct": "1.2",
            "return_1h_pct": "2.5",
        },
        now_ms=100100,
        max_age_ms=60000,
    )["request"]


@pytest.fixture
def isolated_query(isolated_sdk, monkeypatch):
    interpreter, adapter, untrusted = isolated_sdk
    monkeypatch.setattr(transport, "SHARED_PYTHON", str(interpreter))
    monkeypatch.setattr(transport, "__file__", str(adapter))
    monkeypatch.chdir(untrusted)
    monkeypatch.setenv("PYTHONPATH", str(untrusted))
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-never-inherited")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(untrusted))
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-isolated-sdk")
    return transport.SharedJevQuery()


def test_real_isolated_child_needs_only_adapter_contract_and_symbols(isolated_query):
    result = isolated_query(request(), timeout_ms=2000)
    assert result == {
        "model": core.MODEL,
        "answers": {
            name: {"type": "noul", "noul": "0.5"}
            for name in ("trend_supported", "chase_risk", "liquidity_fragile")
        },
        "usage": {"input_tokens": 123, "output_tokens": 10},
    }


@pytest.mark.parametrize(
    "symbol,error",
    [
        ("ETHUSDT", TimeoutError),
        ("SOLUSDT", core.JevProviderUnavailable),
        ("BNBUSDT", core.JevProviderRequestRejected),
        ("XRPUSDT", core.JevResponseInvalid),
    ],
)
def test_real_isolated_child_preserves_typed_failures_without_bodies(
    isolated_query, symbol, error
):
    with pytest.raises(error) as caught:
        isolated_query(request(symbol), timeout_ms=2000)
    assert str(caught.value) == ""


def test_real_isolated_child_preserves_typed_configuration_failure(
    isolated_query, monkeypatch
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-config-unavailable")
    with pytest.raises(core.JevConfigurationUnavailable) as caught:
        isolated_query(request(), timeout_ms=2000)
    assert str(caught.value) == ""


def test_sdk_symbols_no_longer_belong_to_core():
    assert not any(
        hasattr(core, name)
        for name in (
            "SharedJevQuery",
            "SHARED_PYTHON",
            "SDK_VERSION",
            "_bounded_process",
            "_sdk_once",
            "subprocess",
            "Path",
        )
    )
    assert transport.JevResponseInvalid is core.JevResponseInvalid
    assert transport.JevConfigurationUnavailable is core.JevConfigurationUnavailable
    assert transport.JevLocalStartupUnavailable is core.JevLocalStartupUnavailable
    assert transport.JevProviderUnavailable is core.JevProviderUnavailable
    assert transport.JevProviderRequestRejected is core.JevProviderRequestRejected


def test_transport_and_core_share_identical_request_contract():
    prepared = request()
    assert transport._request(prepared["state"]) == prepared
    assert transport._json(prepared) == core._json(prepared)
    assert len(json.dumps(prepared).encode()) < core.MAX_REQUEST_BYTES


def test_real_isolated_child_uses_shared_unicode_symbol_contract(isolated_query):
    assert isolated_query(request("牛来USDT"), timeout_ms=2000)["model"] == core.MODEL


def test_real_isolated_child_import_failure_before_request_is_typed(
    isolated_query, monkeypatch
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-import-unavailable")
    with pytest.raises(core.JevLocalStartupUnavailable) as caught:
        isolated_query(request(), timeout_ms=2000)
    assert str(caught.value) == ""


def test_real_isolated_child_import_failure_after_request_keeps_unknown_usage(
    isolated_query,
):
    with pytest.raises(ValueError, match="JEV_SDK_UNAVAILABLE") as caught:
        isolated_query(request("ADAUSDT"), timeout_ms=2000)
    assert not isinstance(caught.value, core.JevLocalStartupUnavailable)


@pytest.mark.parametrize("failure", ["missing_symbols", "invalid_contract"])
def test_real_release_bootstrap_failure_has_pre_request_proof(
    isolated_sdk, isolated_query, tmp_path, monkeypatch, failure
):
    _, adapter, _ = isolated_sdk
    release = tmp_path / "broken-release"
    shutil.copytree(adapter.parents[1], release)
    if failure == "missing_symbols":
        (release / "v2_core" / "symbols.py").unlink()
    else:
        (release / "v2_core" / "jev_advisory.py").write_text(
            "raise RuntimeError('PRIVATE_IMPORT_DIAGNOSTICS')\n"
        )
    monkeypatch.setattr(transport, "__file__", str(release / "services" / adapter.name))
    with pytest.raises(core.JevLocalStartupUnavailable) as caught:
        isolated_query(request(), timeout_ms=2000)
    assert str(caught.value) == ""


def test_real_no_request_self_check_with_invalid_credentials(isolated_sdk):
    interpreter, adapter, untrusted = isolated_sdk
    result = subprocess.run(
        [str(interpreter), "-I", str(adapter), "--self-check"],
        env={
            "PATH": "/usr/bin:/bin",
            "TYPESAFE_API_KEY": "synthetic-config-unavailable",
            "PYTHONPATH": str(untrusted),
        },
        cwd=untrusted,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout) == {
        "schema": "jev-transport-self-check-v1",
        "status": "READY",
        "isolated": True,
        "contract_ready": True,
        "sdk_version": "0.7.5",
        "provider_request_started": False,
        "credentials_checked": False,
    }


@pytest.mark.parametrize(
    "proof",
    [
        {},
        {"schema": "jev-local-startup-failure-v1", "stage": "SDK_IMPORT"},
        {
            "schema": "jev-local-startup-failure-v1",
            "stage": "SDK_IMPORT",
            "provider_request_started": True,
        },
        {
            "schema": "jev-local-startup-failure-v1",
            "stage": "SDK_REQUEST",
            "provider_request_started": False,
        },
        {
            "schema": "jev-local-startup-failure-v1",
            "stage": [],
            "provider_request_started": False,
        },
    ],
)
def test_exit_seven_alone_is_not_startup_proof(proof):
    program = (
        "import sys; sys.stdout.write(" + repr(json.dumps(proof)) + ");sys.exit(7)"
    )
    with pytest.raises(ValueError, match="JEV_SDK_UNAVAILABLE") as caught:
        transport._bounded_process([sys.executable, "-I", "-c", program], b"", 2000)
    assert not isinstance(caught.value, core.JevLocalStartupUnavailable)


def test_interpreter_launch_failure_is_known_not_requested(tmp_path):
    with pytest.raises(core.JevLocalStartupUnavailable):
        transport._bounded_process([str(tmp_path / "absent-python")], b"", 2000)


@pytest.mark.parametrize("broken_metadata", [False, True])
def test_real_pinned_sdk_check_failure_precedes_client_creation(
    isolated_sdk, isolated_query, tmp_path, monkeypatch, broken_metadata
):
    interpreter, _, _ = isolated_sdk
    broken_venv = tmp_path / "broken-sdk-venv"
    shutil.copytree(interpreter.parents[1], broken_venv, symlinks=True)
    site = (
        broken_venv
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    metadata = site / "typesafe_sdk-0.7.5.dist-info"
    if broken_metadata:
        shutil.rmtree(metadata)
    else:
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: typesafe-sdk\nVersion: 0.7.6\n"
        )
    (site / "jev_shared.py").write_text(
        "raise AssertionError('SDK_PIN_MUST_BE_CHECKED_BEFORE_IMPORT')\n"
    )
    monkeypatch.setattr(transport, "SHARED_PYTHON", str(broken_venv / "bin" / "python"))
    with pytest.raises(core.JevLocalStartupUnavailable):
        isolated_query(request(), timeout_ms=2000)
