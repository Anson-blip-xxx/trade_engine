"""A release probe must not admit signals or disclose its credential."""

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from scripts import v2_trend_tv_probe as module
from v2_core.ingress import SignalIngress
from v2_core.webhook import TradingViewWebhook

SECRET = "qa-only-negative-probe-never-a-production-secret"


def fake_fetch(*, old_parser=False, malicious=False):
    calls = []

    def fetch(endpoint, *, method, body=None):
        calls.append((endpoint, method, body))
        if endpoint == "public_health":
            status, result = 200, {"ok": True, "service": "trade-v2-edge"}
        elif method == "GET":
            status, result = 405, {"error": "METHOD_NOT_ALLOWED"}
        elif body == b"{}":
            status, result = 401, {"error": "AUTHENTICATION_FAILED"}
        else:
            status = 400
            result = {
                "error": "INVALID_ALERT_FIELDS"
                if old_parser
                else "INVALID_PRODUCER_METADATA"
            }
        if malicious:
            result = {
                "error": SECRET,
                "url": "https://example.invalid/?secret=" + SECRET,
            }
        return status, json.dumps(result).encode(), 1

    return fetch, calls


def test_default_probes_do_not_read_secret_or_send_authenticated_payload(monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    fetch, calls = fake_fetch()

    def forbidden():
        raise AssertionError("SECRET_READ_IN_DEFAULT_MODE")

    result = module.probe(fetch=fetch, secret_loader=forbidden)
    assert result["passed"] and len(calls) == 4
    assert all(body in (None, b"{}") for _, _, body in calls)
    assert not result["authenticated_version_proof"]


def test_authenticated_proof_requires_root_before_loader_or_network(monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    fetch, calls = fake_fetch()
    with pytest.raises(PermissionError):
        module.probe(
            authenticated_version_proof=True, fetch=fetch, secret_loader=lambda: SECRET
        )
    assert not calls


def test_authenticated_proof_distinguishes_old_parser_and_never_prints_secret(
    monkeypatch,
):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    for old_parser in (False, True):
        fetch, calls = fake_fetch(old_parser=old_parser)
        result = module.probe(
            authenticated_version_proof=True, fetch=fetch, secret_loader=lambda: SECRET
        )
        assert result["passed"] is not old_parser
        assert len(calls) == 6
        for _, method, body in calls[-2:]:
            payload = json.loads(body)
            assert method == "POST" and payload["producer"] == {}
            assert payload["observed_at"] == 0 and payload["expires_at_ms"] == 1
        assert SECRET not in json.dumps(result)


def test_negative_payload_rejected_by_real_parser_before_any_storage():
    class ForbiddenStore:
        def lookup(self, **kwargs):
            raise AssertionError("STORAGE_ACCESS_FOR_INVALID_PRODUCER")

        def admit(self, **kwargs):
            raise AssertionError("SIGNAL_ADMISSION_FROM_PROBE")

    ingress = SignalIngress(
        ForbiddenStore(),
        source="tv_bridge",
        environment="SANDBOX",
        clock_ms=lambda: 1800000,
        max_age_ms=300000,
        max_lifetime_ms=300000,
    )
    app = TradingViewWebhook(ingress, secret=SECRET)
    body = module.negative_payload(SECRET)
    statuses = []
    response = b"".join(
        app(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/v2/webhooks/tradingview",
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": BytesIO(body),
            },
            lambda status, headers: statuses.append(status),
        )
    )
    assert statuses == ["400 Bad Request"]
    assert json.loads(response) == {"error": "INVALID_PRODUCER_METADATA"}


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666])
def test_secret_rejects_unprotected_file(monkeypatch, tmp_path, mode):
    credential = tmp_path / "credential"
    credential.write_text(SECRET)
    credential.chmod(mode)
    monkeypatch.setattr(module, "SECRET_PATH", str(credential))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="PROTECTED_CREDENTIAL_REQUIRED"):
        module.read_secret()


def test_secret_is_root_only_and_rejects_symlink(monkeypatch, tmp_path):
    credential = tmp_path / "credential"
    credential.write_text(SECRET + "\n")
    credential.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(credential)
    monkeypatch.setattr(module, "SECRET_PATH", str(credential))
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        module.read_secret()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    assert module.read_secret() == SECRET
    monkeypatch.setattr(module, "SECRET_PATH", str(link))
    with pytest.raises(OSError):
        module.read_secret()


@pytest.mark.parametrize(
    "content",
    ["short", "x" * 514, "x" * 32 + "\n\n", "x" * 32 + "\x00", "x" * 32 + "é"],
)
def test_secret_rejects_invalid_bytes(monkeypatch, tmp_path, content):
    credential = tmp_path / "credential"
    credential.write_text(content)
    credential.chmod(0o600)
    monkeypatch.setattr(module, "SECRET_PATH", str(credential))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError):
        module.read_secret()


def test_network_errors_and_hostile_response_content_never_escape_report():
    fetch, _ = fake_fetch(malicious=True)
    result = module.probe(fetch=fetch)
    assert not result["passed"]
    assert SECRET not in json.dumps(result) and "https://" not in json.dumps(result)

    def fail(*args, **kwargs):
        raise OSError("private credential " + SECRET)

    result = module.probe(fetch=fail)
    assert not result["passed"] and len(result["checks"]) == 4
    assert SECRET not in json.dumps(result)


class Response(BytesIO):
    def __init__(self, body, code=200):
        super().__init__(body)
        self.code = code


def test_requester_ignores_proxies_blocks_redirects_and_bounds_io(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response(b"{}", 401)

    def builder(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], module.NoRedirect)
        assert (
            handlers[1].redirect_request(
                None, None, 302, "", {}, "https://evil.invalid"
            )
            is None
        )
        return Opener()

    monkeypatch.setattr(module, "build_opener", builder)
    fetch = module.requester()
    assert fetch("local_webhook", method="POST", body=b"{}")[0] == 401
    assert calls[0][1] == 5
    assert calls[0][0].full_url == module.ENDPOINTS["local_webhook"]
    with pytest.raises(ValueError):
        fetch("https://evil.invalid", method="POST", body=b"{}")
    with pytest.raises(ValueError):
        fetch("public_webhook", method="POST", body=b"x" * 2049)
    with pytest.raises(ValueError):
        fetch("public_webhook", method="GET", body=b"{}")
    assert len(calls) == 1


def test_response_too_large_is_rejected(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return Response(b"x" * (module.MAX_BODY + 1))

    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="RESPONSE_TOO_LARGE"):
        module.requester()("public_health", method="GET")


def test_redirect_status_is_not_followed_or_accepted(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 302, "redirect", {}, BytesIO(b"{}"))

    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    result = module.probe()
    assert not result["passed"]
    assert all(item["http_status"] == 302 for item in result["checks"].values())


def test_main_never_prints_secret_exception(monkeypatch, capsys):
    def fail(**kwargs):
        raise ValueError(SECRET + " https://private.invalid/?secret=" + SECRET)

    monkeypatch.setattr(module, "probe", fail)
    monkeypatch.setattr("sys.argv", ["probe"])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert SECRET not in output and "https://" not in output
    assert json.loads(output)["error_type"] == "ValueError"
