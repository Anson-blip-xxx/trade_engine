"""Disposable, bounded adapter for the pinned shared JEV SDK.

Policy, prompts, response validation and typed failures remain in the pure
core. Only this adapter may launch the shared interpreter or import its SDK.
The child is executed with ``-I``: load the sibling pure contract by an exact
path, without adding the application or an ambient PYTHONPATH to sys.path.
"""

import json
import logging
import os
import selectors
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_LOCAL_STARTUP_SCHEMA = "jev-local-startup-failure-v1"
_LOCAL_STARTUP_STAGES = frozenset({"PURE_CONTRACT_IMPORT", "SDK_PIN", "SDK_IMPORT"})


def _startup_failure(stage):
    """Child-only proof: these callers run before any client/request exists."""
    if stage not in _LOCAL_STARTUP_STAGES:
        raise ValueError("JEV_STARTUP_STAGE_INVALID")
    sys.stdout.buffer.write(
        json.dumps(
            {
                "schema": _LOCAL_STARTUP_SCHEMA,
                "stage": stage,
                "provider_request_started": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    return 7


def _isolated_contract():
    """Load only the pure contract and its explicit pure-symbol dependency.

    The synthetic package has no import search path. Its real __init__ is never
    executed, and no sibling application/DB module becomes importable. A new
    dependency requires an explicit audit here, not a sys.path workaround.
    """
    from importlib.machinery import ModuleSpec
    from importlib.util import module_from_spec, spec_from_file_location
    from types import ModuleType

    root = Path(__file__).resolve().parents[1] / "v2_core"
    package = ModuleType("v2_core")
    package.__path__ = []
    package.__spec__ = ModuleSpec("v2_core", loader=None, is_package=True)
    sys.modules["v2_core"] = package
    symbols_spec = spec_from_file_location("v2_core.symbols", root / "symbols.py")
    symbols = module_from_spec(symbols_spec)
    sys.modules[symbols_spec.name] = symbols
    symbols_spec.loader.exec_module(symbols)
    package.symbols = symbols
    contract_spec = spec_from_file_location(
        "_v2_jev_contract",
        root / "jev_advisory.py",
    )
    contract = module_from_spec(contract_spec)
    # Dataclasses resolve annotations through the module registry. This is
    # a private child-only module, not a shim from the core back to services.
    sys.modules[contract_spec.name] = contract
    contract_spec.loader.exec_module(contract)
    for required in (
        "MODEL",
        "MAX_REQUEST_BYTES",
        "MAX_RESPONSE_BYTES",
        "JevConfigurationUnavailable",
        "JevLocalStartupUnavailable",
        "JevProviderRequestRejected",
        "JevProviderUnavailable",
        "JevResponseInvalid",
        "_integer",
        "_json",
        "_request",
        "parse_response",
    ):
        getattr(contract, required)
    return contract


if __name__ == "__main__":
    try:
        _contract = _isolated_contract()
    except Exception:  # noqa: BLE001 - explicit pure phase, no SDK/client loaded
        raise SystemExit(_startup_failure("PURE_CONTRACT_IMPORT")) from None
else:
    from v2_core import jev_advisory as _contract

MODEL = _contract.MODEL
MAX_REQUEST_BYTES = _contract.MAX_REQUEST_BYTES
MAX_RESPONSE_BYTES = _contract.MAX_RESPONSE_BYTES
JevConfigurationUnavailable = _contract.JevConfigurationUnavailable
JevLocalStartupUnavailable = _contract.JevLocalStartupUnavailable
JevProviderRequestRejected = _contract.JevProviderRequestRejected
JevProviderUnavailable = _contract.JevProviderUnavailable
JevResponseInvalid = _contract.JevResponseInvalid
_integer = _contract._integer
_json = _contract._json
_request = _contract._request
parse_response = _contract.parse_response

SDK_VERSION = "0.7.5"
SHARED_PYTHON = "/home/ubuntu/shared/jev/.venv/bin/python"


def _bounded_process(argv, payload, timeout_ms):
    """Bound output and wall time; kill only the child created by this call."""
    _integer(timeout_ms, 1, 2000)
    if type(payload) is not bytes or len(payload) > MAX_REQUEST_BYTES + 512:
        raise ValueError("JEV_REQUEST_TOO_LARGE")
    deadline = time.monotonic() + timeout_ms / 1000
    # Reserve a small part of the total budget for SIGKILL/reaping; do not add
    # a fresh timeout after an exhausted model call.
    work_deadline = deadline - min(0.1, timeout_ms / 2000)
    # No exchange/TG/credential directory, loader injection, proxy, Python path
    # or service environment follows this child. Shared config has a fixed
    # absolute path, so HOME is unnecessary. Only the shared API-key override
    # is supported; the model is always passed explicitly and cannot drift.
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    if os.environ.get("TYPESAFE_API_KEY"):
        environment["TYPESAFE_API_KEY"] = os.environ["TYPESAFE_API_KEY"]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=environment,
        )
    except OSError:
        # Popen never returned a child: no provider request could be made.
        raise JevLocalStartupUnavailable() from None
    try:
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        output = bytearray()
        sent, finished = 0, False
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while not finished:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                events = selector.select(remaining)
                if not events:
                    raise TimeoutError()
                for ready, _ in events:
                    if ready.fileobj is process.stdin:
                        try:
                            sent += os.write(process.stdin.fileno(), payload[sent:])
                        except BrokenPipeError:
                            # The child may have emitted a typed startup proof
                            # before reading stdin. Drain its bounded stdout.
                            selector.unregister(process.stdin)
                            process.stdin.close()
                            continue
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(
                            process.stdout.fileno(),
                            MAX_RESPONSE_BYTES + 1 - len(output),
                        )
                        if not chunk:
                            finished = True
                            break
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE_BYTES:
                            raise ValueError("JEV_RESPONSE_TOO_LARGE")
        process.wait(timeout=max(0.001, work_deadline - time.monotonic()))
        if process.returncode == 2:
            raise JevProviderUnavailable()
        if process.returncode == 3:
            raise TimeoutError()
        if process.returncode == 4:
            raise JevResponseInvalid()
        if process.returncode == 5:
            raise JevProviderRequestRejected()
        if process.returncode == 6:
            raise JevConfigurationUnavailable()
        if process.returncode == 7:
            try:
                proof = json.loads(output)
                if (
                    type(proof) is dict
                    and set(proof) == {"schema", "stage", "provider_request_started"}
                    and proof["schema"] == _LOCAL_STARTUP_SCHEMA
                    and type(proof["stage"]) is str
                    and proof["stage"] in _LOCAL_STARTUP_STAGES
                    and proof["provider_request_started"] is False
                ):
                    raise JevLocalStartupUnavailable()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        if process.returncode != 0:
            raise ValueError("JEV_SDK_UNAVAILABLE")
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise TimeoutError() from None
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            # Never block a trading host indefinitely even during OS trouble.
            # Popen retains/reaps its own child later; no process-group kill.
            pass
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


class SharedJevQuery:
    """Official pinned shared SDK in a disposable process, not a public gateway."""

    def __call__(self, request, *, timeout_ms):
        _integer(timeout_ms, 1, 2000)
        if request != _request(request.get("state")):
            raise ValueError("JEV_REQUEST_CONTRACT_CHANGED")
        payload = _json({"request": request, "timeout_ms": timeout_ms}).encode()
        raw = _bounded_process(
            [SHARED_PYTHON, "-I", str(Path(__file__).resolve()), "--sdk-once"],
            payload,
            timeout_ms,
        )
        try:
            return parse_response(json.loads(raw))
        except (ValueError, TypeError, KeyError):
            raise JevResponseInvalid() from None


def _sdk_dependencies():
    """Validate local wheel metadata/imports, without constructing a client."""
    try:
        pinned = version("typesafe-sdk") == SDK_VERSION
    except PackageNotFoundError:
        return None, "SDK_PIN"
    if not pinned:
        return None, "SDK_PIN"
    try:
        from jev_shared import ConfigurationError, create_client
        from typesafe_sdk import RetryPolicy
    except ImportError:
        # This narrow import phase precedes client creation. ImportError from
        # system_one below is unknown usage and must never take this path.
        return None, "SDK_IMPORT"
    return (create_client, ConfigurationError, RetryPolicy), None


def _self_check():
    """Local startup check only: no credentials, client creation or API call."""
    dependencies, failed_stage = _sdk_dependencies()
    if failed_stage:
        return _startup_failure(failed_stage)
    sys.stdout.buffer.write(
        _json(
            {
                "schema": "jev-transport-self-check-v1",
                "status": "READY",
                "isolated": sys.flags.isolated == 1,
                "contract_ready": dependencies is not None,
                "sdk_version": SDK_VERSION,
                "provider_request_started": False,
                "credentials_checked": False,
            }
        ).encode()
    )
    return 0


def _sdk_once():
    """Child-only entry: configuration is consumed solely by the shared client."""
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 513)
        if len(raw) > MAX_REQUEST_BYTES + 512:
            raise ValueError("JEV_REQUEST_TOO_LARGE")
        envelope = json.loads(raw)
        if type(envelope) is not dict or set(envelope) != {"request", "timeout_ms"}:
            raise ValueError("JEV_REQUEST_INVALID")
        timeout_ms = _integer(envelope["timeout_ms"], 1, 2000)
        request = envelope["request"]
        if request != _request(request.get("state")):
            raise ValueError("JEV_REQUEST_CONTRACT_CHANGED")
        dependencies, failed_stage = _sdk_dependencies()
        if failed_stage:
            return _startup_failure(failed_stage)
        create_client, ConfigurationError, RetryPolicy = dependencies

        logging.disable(logging.CRITICAL)
        try:
            client = create_client(model=MODEL)
        except Exception as exc:
            if isinstance(exc, ConfigurationError):
                # Missing/invalid local credentials or client configuration,
                # never a response or provider balance inference. No text read.
                return 6
            raise
        with client:
            response = client.system_one(
                **request,
                timeout=timeout_ms / 1000,
                retry=RetryPolicy(max_retries=0, timeout=timeout_ms / 1000),
            )
        try:
            encoded = _json(parse_response(response.model_dump(mode="json"))).encode()
        except (ValueError, TypeError, KeyError):
            return 4
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError("JEV_RESPONSE_TOO_LARGE")
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception as exc:  # noqa: BLE001 - child boundary exports no raw failures
        # No SDK messages, bodies, config, headers or traceback may escape.
        try:
            from typesafe_sdk import (
                TypeSafeAPIConnectionError,
                TypeSafeAPIError,
                TypeSafeAPIResponseValidationError,
                TypeSafeAPITimeoutError,
            )

            if isinstance(exc, TypeSafeAPITimeoutError):
                return 3
            if isinstance(exc, TypeSafeAPIResponseValidationError):
                return 4
            if isinstance(exc, TypeSafeAPIError):
                # SDK's structured HTTP status only: never infer token balance
                # from a body/message or treat an invalid 200 as availability.
                status = getattr(exc, "status", None)
                return 5 if type(status) is int and 400 <= status <= 599 else 1
            if isinstance(exc, TypeSafeAPIConnectionError):
                return 2
        except (ImportError, AttributeError):
            pass
        return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--sdk-once"]:
        raise SystemExit(_sdk_once())
    if sys.argv[1:] == ["--self-check"]:
        try:
            status = _self_check()
        except Exception:  # noqa: BLE001 - no SDK diagnostics on a public CLI
            status = 1
        raise SystemExit(status)
    raise SystemExit(2)
