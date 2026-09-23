import json
from dataclasses import replace

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_trading_pipeline import case as pipeline_case
from test_v2_venue_readiness import Venue

from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_followup import DirectionalFollowupStage
from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_testnet_daemon import (
    TestnetTradingDaemon,
    create_directional_testnet_pipeline,
)
from v2_core.state import BusinessState

database = database_fixture
case = pipeline_case


class Public:
    environment = "SANDBOX"

    def __call__(self, *_):
        raise AssertionError("factory construction performs no public I/O")


def compose(pipeline, **changes):
    request = Venue()
    settings = {
        "runtime": pipeline.runtime,
        "request": request,
        "public_market": Public(),
        "market": pipeline.market,
        "regime": pipeline.regime,
        "schedulers": pipeline.schedulers,
        "mark_reference": lambda _: {},
        "clock_ms": pipeline.runtime.clock_ms,
        "exit_fee_rate": ".0004",
        "enable_entries": False,
        "enable_protection_writes": False,
        "enable_reduce_only_exits": False,
    }
    settings.update(changes)
    return create_directional_testnet_pipeline(**settings)


def test_factory_wires_actual_lifecycle_stages_without_io(case):
    pipeline, calls, *_ = case
    composed = compose(pipeline)
    assert isinstance(composed.protection, DirectionalProtectionStage)
    assert isinstance(composed.exits, DirectionalExitStage)
    assert isinstance(composed.settlement, DirectionalSettlementStage)
    assert isinstance(composed.followups, DirectionalFollowupStage)
    assert composed.enable_entries is False
    assert composed.protection.allow_writes is False
    assert composed.exits.allow_writes is False
    assert calls == []


def test_factory_propagates_external_position_exclusion_to_final_settlement(case):
    pipeline, *_ = case
    composed = compose(pipeline, external_position_exclusions=("ZORAUSDT",))
    assert composed.protection.audit.excluded_position_symbols == ("ZORAUSDT",)
    assert composed.settlement.coverage.excluded_position_symbols == ("ZORAUSDT",)


def test_factory_can_enable_reducing_exits_while_entries_remain_disabled(case):
    pipeline, *_ = case
    pipeline.runtime.execution.submit.reduce_only_enabled = True
    composed = compose(pipeline, enable_reduce_only_exits=True)
    assert composed.enable_entries is False
    assert composed.protection.allow_writes is False
    assert composed.exits.allow_writes is True


def test_factory_requires_and_accepts_protection_with_enabled_entries(case):
    pipeline, *_ = case
    pipeline.runtime.execution.submit.enabled = True
    composed = compose(pipeline, enable_entries=True, enable_protection_writes=True)
    assert composed.enable_entries is True
    assert composed.protection.allow_writes is True
    assert composed.exits.allow_writes is False


@pytest.mark.parametrize(
    "defect",
    ["entry_gate", "exit_gate", "unprotected_entry", "live", "request", "public"],
)
def test_factory_rejects_mismatched_or_unsafe_permissions(case, defect):
    pipeline, *_ = case
    options = {}
    if defect == "entry_gate":
        options["enable_entries"] = True
    elif defect == "exit_gate":
        options["enable_reduce_only_exits"] = True
    elif defect == "unprotected_entry":
        pipeline.runtime.execution.submit.enabled = True
        options["enable_entries"] = True
    elif defect == "live":
        pipeline.runtime.scope = replace(pipeline.scope, environment="LIVE")
    elif defect == "request":
        wrong = Venue()
        wrong.account_id = "other"
        options["request"] = wrong
    else:
        wrong = Public()
        wrong.environment = "LIVE"
        options["public_market"] = wrong
    with pytest.raises(ValueError):
        compose(pipeline, **options)


class Stop:
    def __init__(self, cycles=1):
        self.cycles, self.waits = cycles, []

    def is_set(self):
        return self.cycles <= 0

    def wait(self, seconds):
        self.waits.append(seconds)
        self.cycles -= 1
        return self.is_set()


def test_daemon_checkpoints_before_and_after_pipeline_cycle(
    case, database, monkeypatch
):
    pipeline, *_ = case
    daemon = TestnetTradingDaemon(pipeline, stop=Stop(), notify=lambda _: True)
    calls = []
    monkeypatch.setattr(
        pipeline,
        "run_once",
        lambda: (
            calls.append("pipeline") or {"status": "ENTRY_BLOCKED", "cycle_id": "cycle"}
        ),
    )
    assert daemon.run_once()["cycle_id"] == "cycle"
    assert calls == ["pipeline"]
    state = BusinessState(database).read(daemon.key)
    assert state.version == 2
    payload = json.loads(state.payload_json)
    assert payload["status"] == "RUNNING"
    assert payload["pipeline_status"] == "ENTRY_BLOCKED"
    assert payload["entry_dispatch_enabled"] is False
    assert payload["protection_writes_enabled"] is False
    assert payload["reduce_only_exits_enabled"] is False


def test_checkpoint_failure_prevents_pipeline_launch(case, monkeypatch):
    pipeline, *_ = case
    daemon = TestnetTradingDaemon(pipeline, stop=Stop(), notify=lambda _: True)
    called = []
    monkeypatch.setattr(
        daemon, "_checkpoint", lambda _: (_ for _ in ()).throw(ConnectionError())
    )
    monkeypatch.setattr(pipeline, "run_once", lambda: called.append(True))
    with pytest.raises(ConnectionError):
        daemon.run_once()
    assert called == []


def test_supervisor_sanitizes_failure_and_honors_stop(case, monkeypatch):
    pipeline, *_ = case
    stop, alerts = Stop(cycles=2), []
    daemon = TestnetTradingDaemon(
        pipeline,
        stop=stop,
        notify=lambda event: alerts.append(event),
        interval_seconds=3,
    )
    monkeypatch.setattr(
        daemon, "run_once", lambda: (_ for _ in ()).throw(TimeoutError("secret-key"))
    )
    daemon.serve()
    assert stop.waits == [3, 3]
    assert [event["error_code"] for event in alerts] == ["TimeoutError", "TimeoutError"]
    assert "secret" not in json.dumps(alerts)
