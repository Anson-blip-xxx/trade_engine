"""No-process/no-database checks for the exact recovery release manifest."""

import importlib
from types import SimpleNamespace

import pytest

from scripts import v2_profitability_release_restart as protocol
from scripts import v2_recovery_release_20261115 as release


def reader(before, after):
    return SimpleNamespace(
        manifest=lambda root: before if root == release.PRIOR else after
    )


def test_empty_manifest_never_deploys(monkeypatch):
    monkeypatch.setattr(release, "EXPECTED", {})
    with pytest.raises(ValueError, match="EXACT_TESTED_MANIFEST_REQUIRED"):
        release.reviewed_manifest(reader({"base": "x"}, {"base": "x"}))


def test_exact_only_reviewed_delta(monkeypatch):
    monkeypatch.setattr(release, "EXPECTED", {"changed": "new"})
    release.reviewed_manifest(
        reader({"base": "x", "changed": "old"}, {"base": "x", "changed": "new"})
    )


@pytest.mark.parametrize(
    "after",
    [
        {"changed": "new"},
        {"base": "x", "changed": "wrong"},
        {"base": "x", "changed": "new", "unreviewed": "z"},
        {"base": "different", "changed": "new"},
    ],
)
def test_missing_extra_or_tampered_artifact_rejected(monkeypatch, after):
    monkeypatch.setattr(release, "EXPECTED", {"changed": "new"})
    with pytest.raises(ValueError, match="EXACT_TESTED_MANIFEST_REQUIRED"):
        release.reviewed_manifest(reader({"base": "x", "changed": "old"}, after))


def test_unsupported_args_fail_before_service_access(monkeypatch):
    monkeypatch.setattr(release.sys, "argv", ["release", "--force"])
    with pytest.raises(ValueError, match="EXPECTED_DRY_RUN_OR_EXECUTE"):
        release.main()


def test_no_existing_controller_environment_no_restart(monkeypatch):
    monkeypatch.setattr(release.sys, "argv", ["release"])
    monkeypatch.delenv("V2_SHARED_MARKET", raising=False)
    with pytest.raises(
        ValueError, match="EXISTING_SHARED_CONTROLLER_ENVIRONMENT_REQUIRED"
    ):
        release.main()


def test_import_program_obeys_restart_verifier_contract(monkeypatch, tmp_path, capsys):
    prior, candidate, link = (
        tmp_path / "prior",
        tmp_path / "candidate",
        tmp_path / "controller",
    )
    prior.mkdir()
    candidate.mkdir()
    link.symlink_to(prior, target_is_directory=True)
    monkeypatch.setattr(protocol, "PRIOR", prior)
    monkeypatch.setattr(protocol, "CANDIDATE", candidate)
    monkeypatch.setattr(protocol, "LINK", link)
    monkeypatch.setattr(protocol, "IMPORT_PROGRAM", release.IMPORT_PROGRAM)
    monkeypatch.setattr(
        protocol,
        "unit_properties",
        lambda: {
            "ActiveState": "active",
            "SubState": "running",
            "KillMode": "mixed",
            "TimeoutStopUSec": "1min 30s",
            "WorkingDirectory": str(link),
        },
    )
    monkeypatch.chdir(candidate)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: SimpleNamespace(
            __file__=str(candidate / (name.replace(".", "/") + ".py")),
        ),
    )

    def run(command, **kwargs):
        if command[0] == "systemctl":
            return "inactive"
        assert kwargs["cwd"] == candidate
        assert command[-1] == release.IMPORT_PROGRAM
        exec(command[-1], {})  # noqa: S102 - fixed reviewed import-only program
        return capsys.readouterr().out

    monkeypatch.setattr(protocol, "run", run)
    protocol.verify_layout()
