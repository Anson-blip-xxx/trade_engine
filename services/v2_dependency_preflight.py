"""Bounded readiness gate for the fixed isolated V2 deployment endpoints."""

import argparse
import http.client
import importlib.metadata
import socket
import sys
import time

_RUNTIME_REQUIREMENTS = {
    "backports.zstd": "1.7.0",
    "certifi": "2026.7.22",
    "clickhouse-connect": "1.6.0",
    "lz4": "4.4.5",
    "psycopg": "3.3.6",
    "psycopg-binary": "3.3.6",
    "redis": "8.0.1",
    "typing_extensions": "4.16.0",
    "urllib3": "2.8.0",
}


def runtime_ready(
    *, version=importlib.metadata.version, python_version=sys.version_info
):
    """Reject an untested interpreter or data driver before external I/O."""
    if tuple(python_version[:2]) != (3, 12):
        return False
    try:
        return all(
            version(name) == expected
            for name, expected in _RUNTIME_REQUIREMENTS.items()
        )
    except importlib.metadata.PackageNotFoundError:
        return False


def postgres_ready():
    from services.v2_testnet_inventory import deployment_database

    deployment_database()
    return True


def redis_ready():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        client.connect("/var/lib/trade-engine-v2/redis.sock")
        client.sendall(b"*1\r\n$4\r\nPING\r\n")
        return client.recv(32) == b"+PONG\r\n"


def clickhouse_ready():
    connection = http.client.HTTPConnection("127.0.0.1", 18123, timeout=1)
    try:
        connection.request("GET", "/ping", headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(16)
        return response.status == 200 and body.strip() == b"Ok."
    finally:
        connection.close()


def wait_for_dependencies(
    probes=(runtime_ready, postgres_ready, redis_ready, clickhouse_ready),
    *,
    monotonic=time.monotonic,
    sleep=time.sleep,
    timeout_seconds=30,
):
    if (
        not isinstance(probes, (list, tuple))
        or not probes
        or not all(callable(probe) for probe in probes)
        or not callable(monotonic)
        or not callable(sleep)
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 60
    ):
        raise ValueError("bounded dependency readiness policy required")
    started = monotonic()
    while True:
        ready = True
        for probe in probes:
            try:
                ready = probe() is True and ready
            except Exception:  # noqa: BLE001 - readiness emits no endpoint details
                ready = False
        if ready:
            return True
        now = monotonic()
        if now < started or now - started >= timeout_seconds:
            return False
        sleep(min(0.5, timeout_seconds - (now - started)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-only", action="store_true")
    args = parser.parse_args(argv)
    ready = runtime_ready() if args.runtime_only else wait_for_dependencies()
    raise SystemExit(0 if ready else 1)


if __name__ == "__main__":
    main()
