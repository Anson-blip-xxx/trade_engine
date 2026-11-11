from scripts import v2_native_close_precision_release_restart as release


def test_native_precision_uses_existing_guarded_controller_protocol(monkeypatch):
    protocol = release.protocol
    for name in ("PRIOR", "CANDIDATE", "IMPORT_PROGRAM"):
        monkeypatch.setattr(protocol, name, getattr(protocol, name))
    calls = []
    monkeypatch.setattr(protocol, "main", lambda: calls.append(True) or 7)
    assert release.main() == 7
    assert calls == [True]
    assert protocol.PRIOR.name == "mover-symbol-controller-20261102-v1"
    assert protocol.CANDIDATE.name == "native-close-precision-controller-20261105-v1"
    assert "v2_core.native_entry_precision" in protocol.IMPORT_PROGRAM
    assert "services.v2_directional_cash" in protocol.IMPORT_PROGRAM
    assert protocol.UNIT == "trade-v2-execution-controller.service"
