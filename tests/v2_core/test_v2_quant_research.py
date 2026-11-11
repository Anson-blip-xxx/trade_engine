from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from test_v2_quant_indicators import minutes

from services.v2_quant_research import QuantEvidenceContext, QuantPolicy, QuantResearch
from tests.support.v2_admission import database as database_fixture
from v2_core.signals import Signals

database = database_fixture


class Archive:
    environment = "SANDBOX"

    def __init__(self, rows):
        self.rows = list(reversed(rows))

    def read(self, symbol, start, end):
        return [r for r in self.rows if start <= r["t"] < end]


def setup(database, *, mode="CANDIDATE"):
    rows = minutes(breakout=True)
    now = rows[-1]["t"] + 61000
    clock = [now]
    service = QuantResearch(
        database,
        archive=Archive(rows),
        environment="SANDBOX",
        clock_ms=lambda: clock[0],
    )
    policy = QuantPolicy(mode=mode, timeframes=(5,))
    return service, list(reversed(rows)), clock, policy


def observe(case):
    service, rows, _clock, policy = case
    return service.observe("BTCUSDT", rows, 86400000, policy=policy)


def snapshot(database, signal_id):
    with database() as conn:
        return conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal_id,)
        ).fetchone()[0]


def test_shadow_then_publish_replay_without_second_observation(database):
    case = setup(database, mode="SHADOW")
    first = observe(case)["observations"][0]
    assert first["publication"] == "SHADOW" and first["signal_id"] is None
    service, rows, clock, _ = case
    case = service, rows, clock, QuantPolicy(mode="CANDIDATE", timeframes=(5,))
    published = observe(case)["observations"][0]
    assert published["observation_id"] == first["observation_id"]
    assert published == observe(case)["observations"][0]
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_inbound_signals WHERE source='quant'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='quant-observation-v1'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_concurrent_publication_is_idempotent(database):
    case = setup(database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: observe(case), range(4)))
    assert len({r["observations"][0]["signal_id"] for r in results}) == 1


def test_crash_between_record_and_signal_is_recoverable(database, monkeypatch):
    case = setup(database)
    original = Signals.admit

    def crash(*args, **kwargs):
        raise TimeoutError("test crash")

    monkeypatch.setattr(Signals, "admit", crash)
    with pytest.raises(TimeoutError):
        observe(case)
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='quant-observation-v1'"
            ).fetchone()[0]
            == 1
        )
    monkeypatch.setattr(Signals, "admit", original)
    assert observe(case)["observations"][0]["signal_id"]


def test_no_expired_chase_after_crash_or_mode_change(database):
    case = setup(database, mode="SHADOW")
    observe(case)
    service, rows, clock, _ = case
    clock[0] += 100000
    # Current 1m context can advance while the same last 5m bar has expired.
    newer = minutes(1442)
    rows = list(reversed(newer[-2:])) + rows
    clock[0] = 86521000
    result = service.observe(
        "BTCUSDT", rows, 86520000, policy=QuantPolicy(mode="CANDIDATE", timeframes=(5,))
    )
    assert result["observations"][0]["publication"] == "EXPIRED_NOT_PUBLISHED"


def test_evidence_scope_and_signal_tampering_rejected_before_account_reads(database):
    published = observe(setup(database))["observations"][0]
    signal = snapshot(database, published["signal_id"])
    calls = []
    context = QuantEvidenceContext(
        database,
        lambda s: calls.append(s) or {"account": "isolated-QA"},
        archive=Archive(minutes(breakout=True)),
    )
    assert context(signal)["quant"]["result"]["winner"]["strategy_id"] == "donchian"
    for changes in (
        {"symbol": "ETHUSDT"},
        {"signal": "QUANT_DONCHIAN_DOWN"},
        {"observed_at": signal["observed_at"] - 1},
    ):
        altered = {**signal, **changes}
        with pytest.raises(ValueError):
            context(altered)
    altered = deepcopy(signal)
    altered["features"]["quant_target_move_pct"] = "100"
    with pytest.raises(ValueError):
        context(altered)
    assert len(calls) == 1
    with pytest.raises(ValueError):
        QuantEvidenceContext(
            database, lambda _: {}, archive=Archive([]), environment="LIVE"
        )(signal)


def test_off_mode_no_state_and_policy_compare_and_swap(database):
    case = setup(database, mode="OFF")
    assert observe(case)["status"] == "DISABLED"
    policy = QuantPolicy(mode="SHADOW")
    assert (
        policy.save(
            database, "SANDBOX", expected_version=0, request_key="activate"
        ).code
        == "APPLIED"
    )
    assert QuantPolicy.read(database, "SANDBOX").mode == "SHADOW"
    assert (
        policy.save(database, "SANDBOX", expected_version=0, request_key="stale").code
        != "APPLIED"
    )
    with pytest.raises(ValueError):
        QuantPolicy(mode="CANDIDATE").save(
            database, "LIVE", expected_version=0, request_key="forbidden"
        )


def test_missing_history_does_not_poison_later_archive_recovery(database):
    service, rows, clock, policy = setup(database)
    service.archive.rows = rows[:10]
    assert (
        service.observe("BTCUSDT", rows[:10], 86400000, policy=policy)["observations"][
            0
        ]["status"]
        == "WARMUP"
    )
    service.archive.rows = rows
    assert observe((service, rows, clock, policy))["observations"][0]["signal_id"]


def test_corrupt_cache_observation_cannot_pass_independent_archive(database):
    case = setup(database)
    service, rows, _clock, policy = case
    original_archive = Archive(deepcopy(list(reversed(rows))))
    rows[0]["h"] = "102.5"
    signal_id = service.observe("BTCUSDT", rows, 86400000, policy=policy)[
        "observations"
    ][0]["signal_id"]
    assert signal_id
    calls = []
    context = QuantEvidenceContext(
        database, lambda _: calls.append(1), archive=original_archive
    )
    with pytest.raises(ValueError, match="ARCHIVE_PROOF_MISMATCH"):
        context(snapshot(database, signal_id))
    assert calls == []
