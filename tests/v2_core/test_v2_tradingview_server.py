"""Deployment host credential and listener safety checks."""

import os

import pytest

from services.v2_tradingview_server import load_config


def test_loads_protected_systemd_credential_without_mutating_process_environment(
    tmp_path,
):
    source = tmp_path / "tradingview-webhook-secret"
    source.write_text("a" * 64 + "\n")
    source.chmod(0o600)
    before = dict(os.environ)
    config = load_config({"CREDENTIALS_DIRECTORY": str(tmp_path)})
    assert config["V2_TV_WEBHOOK_SECRET"] == "a" * 64
    assert dict(os.environ) == before


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_rejects_readable_secret(tmp_path, mode):
    source = tmp_path / "tradingview-webhook-secret"
    source.write_text("a" * 64 + "\n")
    source.chmod(mode)
    with pytest.raises(ValueError, match="protected webhook credential"):
        load_config({"CREDENTIALS_DIRECTORY": str(tmp_path)})


def test_rejects_symlink_credential(tmp_path):
    source = tmp_path / "source"
    source.write_text("a" * 64 + "\n")
    source.chmod(0o600)
    (tmp_path / "tradingview-webhook-secret").symlink_to(source)
    with pytest.raises(ValueError, match="protected webhook credential"):
        load_config({"CREDENTIALS_DIRECTORY": str(tmp_path)})


def test_rejects_invalid_or_missing_secret(tmp_path):
    source = tmp_path / "tradingview-webhook-secret"
    source.write_text("short\n")
    source.chmod(0o600)
    with pytest.raises(ValueError, match="protected webhook credential"):
        load_config({"CREDENTIALS_DIRECTORY": str(tmp_path)})
    source.write_text("a" * 32 + "\nb")
    with pytest.raises(ValueError, match="invalid webhook credential"):
        load_config({"CREDENTIALS_DIRECTORY": str(tmp_path)})
    with pytest.raises(ValueError, match="systemd credential directory"):
        load_config({})
