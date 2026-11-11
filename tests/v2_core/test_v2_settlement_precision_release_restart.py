from scripts import v2_settlement_precision_release_restart as release


def test_hotfix_keeps_existing_guarded_protocol(monkeypatch):
    protocol = release.protocol
    for name in ("PRIOR", "CANDIDATE", "IMPORT_PROGRAM"):
        monkeypatch.setattr(protocol, name, getattr(protocol, name))
    calls = []
    monkeypatch.setattr(protocol, "main", lambda: calls.append(True) or 7)
    assert release.main() == 7
    assert calls == [True]
    assert protocol.PRIOR.name == "profitability-foundation-controller-20261026-v1"
    assert protocol.CANDIDATE.name == "settlement-entry-witness-controller-20261029-v1"
    assert "services.v2_directional_cash" in protocol.IMPORT_PROGRAM
    assert "services.v2_settlement_entry" in protocol.IMPORT_PROGRAM
    assert protocol.UNIT == "trade-v2-execution-controller.service"
