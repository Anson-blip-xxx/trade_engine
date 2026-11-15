"""The second hotfix cannot expand the first release's restart permissions."""

from types import SimpleNamespace

from scripts import v2_health_lane_release_20261115 as release


def test_only_manifest_roots_and_imports_change(monkeypatch):
    calls = []
    protocol = SimpleNamespace(
        IMPORT_PROGRAM="'services.v2_directional_context',",
        main=lambda: calls.append("guarded_restart") or 7,
    )
    loader = SimpleNamespace(exec_module=lambda module: None)
    monkeypatch.setattr(
        release.importlib.util,
        "spec_from_file_location",
        lambda name, path: SimpleNamespace(loader=loader),
    )
    monkeypatch.setattr(
        release.importlib.util, "module_from_spec", lambda spec: protocol
    )
    assert release.main() == 7
    assert calls == ["guarded_restart"]
    assert protocol.PRIOR == release.PRIOR
    assert protocol.CANDIDATE == release.CANDIDATE
    assert protocol.EXPECTED == release.EXPECTED
    assert "services.v2_trading_health" in protocol.IMPORT_PROGRAM
    assert set(vars(protocol)) == {
        "IMPORT_PROGRAM", "main", "PRIOR", "CANDIDATE", "EXPECTED"
    }
