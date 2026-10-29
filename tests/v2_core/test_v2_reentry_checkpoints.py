import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from threading import Barrier

import pytest
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture
from test_v2_reentry import bar

from services.v2_reentry_archive import ReadLimits, ReentryArchive
from services.v2_reentry_checkpoints import ReentryCheckpoints
from v2_core.quant_indicators import Bar
from v2_core.reentry import ReentryPolicy, assess_reentry
from v2_core.reentry_checkpoint import advance, assess, seed, verify

database = database_fixture
PREVIOUS = {
    "previous_episode": "prior",
    "side": "BUY",
    "exit_reason": "PEAK_DRAWDOWN",
    "exit_known_ms": 25 * 60000,
    "exit_price": "100",
}


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize(
    "reason", ["PEAK_DRAWDOWN", "EMERGENCY_LOSS", "LOW_YIELD_STAGNATION"]
)
@pytest.mark.parametrize("chunk", [1, 7, 120])
def test_checkpoint_matches_full_rule_across_arbitrary_partitions(side, reason, chunk):
    rng = random.Random(147)
    bars = [bar(i) for i in range(25)]
    px = Decimal(100)
    for i in range(25, 240):
        close = px + Decimal(rng.choice([-15, -5, 0, 5, 15])) / 10
        bars.append(
            bar(
                i,
                o=str(px),
                h=str(max(px, close) + 1),
                l=str(min(px, close) - 1),
                c=str(close),
                v=str(rng.choice([5, 10, 20, 40])),
            )
        )
        px = close
    if side == "SELL":
        bars = [Bar(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in bars]
    previous = {**PREVIOUS, "side": side, "exit_reason": reason}
    policy = ReentryPolicy(volume_period=7, atr_period=10, anchor_bars=15)
    state = seed(previous, bars[:25], policy=policy)
    for end in range(25, len(bars), chunk):
        new = bars[end : end + chunk]
        through = new[-1].t + 60000
        # JSON serialization and low caller precision simulate process restarts.
        with localcontext() as ctx:
            ctx.prec = 6
            state = advance(json.loads(json.dumps(state)), new, through_ms=through)
            actual = assess(state, signal_observed_ms=through, as_of_ms=through)
        expected = assess_reentry(
            bars=bars[: through // 60000],
            policy=policy,
            signal_observed_ms=through,
            as_of_ms=through,
            **previous,
        )
        for field in (
            "status",
            "reason",
            "path",
            "opportunity_token",
            "trigger_closed_ms",
            "reset_closed_ms",
            "relative_volume",
            "structure_invalidation_price",
        ):
            assert actual.get(field) == expected.get(field), (field, actual, expected)
        assert len(state["history"]) <= policy.volume_period


def test_multiple_days_do_not_time_unlock_or_change_original_anchor():
    state = seed(PREVIOUS, [bar(i) for i in range(25)])
    original = (state["atr"], state["boundary"])
    for start in range(25, 4025, 1000):
        state = advance(
            state,
            [bar(i) for i in range(start, start + 1000)],
            through_ms=(start + 1000) * 60000,
        )
    assert (
        assess(state, signal_observed_ms=4025 * 60000, as_of_ms=4025 * 60000)["status"]
        == "WAIT"
    )
    assert (state["atr"], state["boundary"]) == original
    state = advance(
        state, [bar(4025, h="102", c="101.5", v="20")], through_ms=4026 * 60000
    )
    assert (
        assess(state, signal_observed_ms=4026 * 60000, as_of_ms=4026 * 60000)["status"]
        == "READY"
    )
    assert (
        assess(state, signal_observed_ms=4025 * 60000, as_of_ms=4026 * 60000)["status"]
        == "WAIT"
    )


def test_gap_tamper_and_future_cannot_authorize():
    state = seed(PREVIOUS, [bar(i) for i in range(25)])
    with pytest.raises(ValueError):
        advance(state, [bar(26)], through_ms=27 * 60000)
    with pytest.raises(ValueError):
        verify({**state, "peak": "999"})
    assert (
        assess(state, signal_observed_ms=26 * 60000, as_of_ms=26 * 60000)["status"]
        == "UNKNOWN"
    )


@pytest.fixture
def checkpoint_db(database):
    with database() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20261002_reentry_checkpoints.sql"
            ).read_text()
        )
    return database


def reader(calls, missing=None):
    def read(target, start, end):
        calls.append((target, start, end))
        return [
            bar(i).payload()
            for i in reversed(range(start // 60000, end // 60000))
            if i != missing
        ]

    return read


def test_pg_pages_resume_and_cannot_skip_to_present(checkpoint_db):
    calls = []
    store = ReentryCheckpoints(
        checkpoint_db, reader(calls), scope=SCOPE, clock_ms=lambda: 3000 * 60000
    )
    args = {
        "target": "BTCUSDT",
        "previous": PREVIOUS,
        "policy": ReentryPolicy(),
        "now": 3000 * 60000,
        "page_bars": 1000,
    }
    first, state = store.prepare(**args)
    assert first["status"] == "UNKNOWN" and state["through_ms"] == 1025 * 60000
    assert len(calls) == 1
    rebuilt = ReentryCheckpoints(
        checkpoint_db, reader(calls), scope=SCOPE, clock_ms=lambda: 3000 * 60000
    )
    status, state = rebuilt.prepare(**args, max_pages=2)
    assert status["status"] == "CURRENT" and status["checkpoint_version"] == 3
    count = len(calls)
    assert rebuilt.prepare(**args)[0] == status
    assert len(calls) == count
    assert (
        assess(state, signal_observed_ms=args["now"], as_of_ms=args["now"])["status"]
        == "WAIT"
    )
    with checkpoint_db() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_reentry_checkpoints").fetchone()[0] == 1
        )
    other = ReentryCheckpoints(
        checkpoint_db,
        reader(calls),
        scope=replace(SCOPE, account_id="other"),
        clock_ms=lambda: args["now"],
    )
    assert other.prepare(**args)[1]["through_ms"] == 1025 * 60000


def test_gap_never_advances_saved_watermark(checkpoint_db):
    store = ReentryCheckpoints(
        checkpoint_db, reader([], missing=50), scope=SCOPE, clock_ms=lambda: 100 * 60000
    )
    result = store.evaluate(
        {"symbol": "BTCUSDT", "observed_at": 100 * 60000},
        previous=PREVIOUS,
        policy=ReentryPolicy(),
        now=100 * 60000,
        page_bars=120,
    )
    assert result["status"] == "UNKNOWN"
    with checkpoint_db() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_reentry_checkpoints").fetchone()[0] == 0
        )


def test_concurrent_preparers_cas_not_last_writer_wins(checkpoint_db):
    barrier = Barrier(2)

    def read(*args):
        barrier.wait(timeout=10)
        return reader([])(*args)

    def run(_):
        store = ReentryCheckpoints(
            checkpoint_db, read, scope=SCOPE, clock_ms=lambda: 30 * 60000
        )
        return store.prepare(
            target="BTCUSDT",
            previous=PREVIOUS,
            policy=ReentryPolicy(),
            now=30 * 60000,
            page_bars=120,
        )[0]["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ["CURRENT", "UNKNOWN"]
    with checkpoint_db() as c:
        assert (
            c.execute("SELECT version FROM v2_reentry_checkpoints").fetchone()[0] == 1
        )


def test_dedicated_archive_limits_no_retry_and_lazy_cleanup():
    created = []

    class Client:
        closed = False

        def query(self, *args, **kwargs):
            raise TimeoutError("private detail")

        def close(self):
            self.closed = True

    client = Client()

    def factory(**kwargs):
        created.append(kwargs)
        return client

    archive = ReentryArchive(factory=factory)
    assert not created
    with pytest.raises(TimeoutError):
        archive.read("BTCUSDT", 0, 60000)
    assert client.closed and archive.client is None
    assert created[0]["query_retries"] == 0
    assert created[0]["send_receive_timeout"] == 2
    assert created[0]["settings"]["readonly"] == 1
    assert created[0]["settings"]["result_overflow_mode"] == "throw"
    with pytest.raises(ValueError):
        ReadLimits(execution_seconds=3, receive_seconds=1)


def test_exit_straddling_bar_is_not_repair_and_policy_change_has_new_anchor(
    checkpoint_db,
):
    previous = {**PREVIOUS, "exit_known_ms": 25 * 60000 + 1}
    before = [bar(i) for i in range(25)] + [bar(25, h="106", l="97", c="102", v="30")]
    state = seed(previous, before)
    assert state["through_ms"] == 26 * 60000
    assert (
        assess(state, signal_observed_ms=26 * 60000, as_of_ms=26 * 60000)["reason"]
        == "NO_POST_EXIT_CLOSED_BAR"
    )
    calls = []
    store = ReentryCheckpoints(
        checkpoint_db, reader(calls), scope=SCOPE, clock_ms=lambda: 30 * 60000
    )
    args = {
        "target": "BTCUSDT",
        "previous": PREVIOUS,
        "now": 30 * 60000,
        "page_bars": 120,
    }
    first = store.prepare(**args, policy=ReentryPolicy())[0]
    changed = store.prepare(**args, policy=ReentryPolicy(reset_atr="0.6"))[0]
    assert first["checkpoint_id"] != changed["checkpoint_id"]
    assert first["checkpoint_version"] == changed["checkpoint_version"] == 1


def test_gate_uses_current_checkpoint_without_replaying_archive(
    checkpoint_db, monkeypatch
):
    from dataclasses import asdict

    from services import v2_reentry_gate as module
    from v2_core.runtime_policy import PolicyStore

    now = 26 * 60000
    bars = [bar(i) for i in range(25)] + [bar(25, h="102", c="101.5", v="20")]
    store = ReentryCheckpoints(
        checkpoint_db,
        lambda _, start, end: [
            b.payload() for b in reversed(bars) if start <= b.t < end
        ],
        scope=SCOPE,
        clock_ms=lambda: now,
    )
    assert (
        store.prepare(
            target="BTCUSDT",
            previous=PREVIOUS,
            policy=ReentryPolicy(),
            now=now,
            page_bars=120,
        )[0]["status"]
        == "CURRENT"
    )
    PolicyStore(checkpoint_db, SCOPE).patch(
        {"entry.reentry_enabled": True}, expected_version=0, reason="isolated QA"
    )
    monkeypatch.setattr(module, "prior_close", lambda *_: (PREVIOUS, None))
    provider = module.StructuralReentryContext(
        checkpoint_db,
        lambda _: {"assembled_at": now, "valid_until_ms": now + 10000},
        scope=SCOPE,
        strategy="S6",
        read_bars=lambda *_: pytest.fail("checkpoint is current"),
        clock_ms=lambda: now,
    )
    proof = provider(
        {"symbol": "BTCUSDT", "observed_at": now, "expires_at_ms": now + 10000}
    )["reentry"]
    assert proof["assessment"]["status"] == "READY"
    assert (
        module.validate(
            proof,
            scope=asdict(SCOPE),
            target="BTCUSDT",
            side="BUY",
            observed=now,
            now=now,
        )
        is None
    )


def test_preparation_failure_rotates_to_other_symbols(checkpoint_db, monkeypatch):
    from types import SimpleNamespace

    from test_v2_account_risk import prepare

    from services import v2_reentry_prepare_entry as module

    for target in ("BTCUSDT", "ETHUSDT"):
        _, data, order, _ = prepare(checkpoint_db, symbol=target)
        data.orders.transition(
            order, expected_version=1, status="SUBMITTING", evidence={}
        )
        data.ledger.record_fill(
            order_id=order,
            exchange_fill_id=target,
            quantity="0.01",
            price="100",
            fee="0",
            fee_currency="USDT",
            occurred_at_ms=1,
            evidence={"source": "qa"},
        )
    seen = []

    def fail(conn, scope, target, now):
        seen.append(target)
        raise TimeoutError("private detail")

    monkeypatch.setattr(module, "prior_close", fail)
    archive = SimpleNamespace(read=lambda *_: pytest.fail("no archive on failed prior"))
    for _ in range(2):
        result = module.prepare_batch(
            checkpoint_db,
            archive,
            scope=SCOPE,
            batch=1,
            pages=1,
            clock_ms=lambda: 30 * 60000,
        )
        assert result["outcomes"] == {"UNAVAILABLE": 1}
    assert seen == ["BTCUSDT", "ETHUSDT"]


def test_release_preflight_reads_schema_without_executing_code(tmp_path):
    from scripts.v2_entry_release_preflight import inspect

    for name, body in (
        ("new", "SCHEMA={'entry.enabled':(False,None,None)}"),
        ("old", "SCHEMA={'old':(False,None,None)}"),
    ):
        target = tmp_path / name / "v2_core"
        target.mkdir(parents=True)
        (target / "runtime_policy.py").write_text(
            "raise RuntimeError('must never execute')\n" + body
        )
    assert inspect(tmp_path / "new", [tmp_path / "new"])["status"] == "COMPATIBLE"
    report = inspect(tmp_path / "new", [tmp_path / "old"])
    assert report["status"] == "BLOCKED" and report["readers"][0]["missing"] == [
        "entry.enabled"
    ]
    (tmp_path / "old/v2_core/runtime_policy.py").write_text(
        "SCHEMA={'entry.enabled':(0,None,None)}"
    )
    report = inspect(tmp_path / "new", [tmp_path / "old"])
    assert report["status"] == "BLOCKED" and report["readers"][0]["changed"] == [
        "entry.enabled"
    ]


def test_terminal_partial_fills_use_weighted_anchor_not_arbitrary_price(
    database, monkeypatch
):
    from test_v2_directional_cash import closed
    from test_v2_directional_settlement import baseline, service

    from services.v2_reentry_gate import prior_close
    from v2_core.ledger import Ledger

    original = Ledger.record_fill

    def record(self, **kwargs):
        if kwargs["exchange_fill_id"] != "2":
            return original(self, **kwargs)
        for fill_id, qty, price, fee, pnl in (
            ("2", "0.25", "88", "0.011", "-3"),
            ("3", "1", "93", "0.035", "-7"),
        ):
            original(
                self,
                **{
                    **kwargs,
                    "exchange_fill_id": fill_id,
                    "quantity": qty,
                    "price": price,
                    "fee": fee,
                    "evidence": {
                        **kwargs["evidence"],
                        "trade_id": fill_id,
                        "venue_realized_pnl": pnl,
                    },
                },
            )

    monkeypatch.setattr(Ledger, "record_fill", record)
    runtime, episode, income = closed.__wrapped__(database)
    income.rows[1]["income"] = "-0.011"
    income.rows[2]["income"] = "-3"
    income.rows += [
        {**income.rows[1], "tradeId": "3", "tranId": 5, "income": "-0.035"},
        {**income.rows[2], "tradeId": "3", "tranId": 6, "income": "-7"},
    ]
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"
    with database() as c:
        previous, missing = prior_close(c, SCOPE, "BTCUSDT", 2**53 - 1)
    assert missing is None and Decimal(previous["exit_price"]) == 92


@pytest.mark.parametrize("ahead", [False, True])
def test_gate_aligns_valid_market_cutoff_but_never_rewinds_newer_checkpoint(
    checkpoint_db, monkeypatch, ahead
):
    from services import v2_reentry_gate as module
    from v2_core.runtime_policy import PolicyStore

    now = 27 * 60000 + 10000
    observed = 26 * 60000
    bars = [bar(i) for i in range(25)] + [bar(25, h="102", c="101.5", v="20"), bar(26)]
    store = ReentryCheckpoints(
        checkpoint_db,
        lambda _, start, end: [
            b.payload() for b in reversed(bars) if start <= b.t < end
        ],
        scope=SCOPE,
        clock_ms=lambda: now,
    )
    store.prepare(
        target="BTCUSDT",
        previous=PREVIOUS,
        policy=ReentryPolicy(),
        now=now,
        cutoff_ms=27 * 60000 if ahead else observed,
        page_bars=120,
    )
    PolicyStore(checkpoint_db, SCOPE).patch(
        {"entry.reentry_enabled": True}, expected_version=0, reason="isolated QA"
    )
    monkeypatch.setattr(module, "prior_close", lambda *_: (PREVIOUS, None))
    context = {
        "assembled_at": now,
        "valid_until_ms": now + 100000,
        "sources": {"s3": {"observed_at": observed}},
    }
    provider = module.StructuralReentryContext(
        checkpoint_db,
        lambda _: context,
        scope=SCOPE,
        strategy="S6",
        read_bars=lambda *_: pytest.fail(
            "must not ask for a newer bar than validated context"
        ),
        clock_ms=lambda: now,
    )
    proof = provider(
        {"symbol": "BTCUSDT", "observed_at": now, "expires_at_ms": now + 100000}
    )["reentry"]
    assert proof["assessment"]["status"] == ("WAIT" if ahead else "READY")
    assert proof["market_cutoff_ms"] == observed
    assert proof["valid_until_ms"] == observed + 90000
    assert proof["assessment"]["bar_cutoff_ms"] == (27 * 60000 if ahead else observed)


def test_explicit_bar_cutoff_cannot_extend_market_lifetime():
    state = advance(
        seed(PREVIOUS, [bar(i) for i in range(25)]),
        [bar(25, h="102", c="101.5", v="20")],
        through_ms=26 * 60000,
    )
    assert (
        assess(
            state,
            signal_observed_ms=28 * 60000,
            as_of_ms=28 * 60000,
            bar_cutoff_ms=26 * 60000,
        )["reason"]
        == "REENTRY_MARKET_STALE"
    )
    with pytest.raises(ValueError, match="REENTRY_BAR_CLOCK"):
        assess(
            state,
            signal_observed_ms=26 * 60000,
            as_of_ms=26 * 60000,
            bar_cutoff_ms=27 * 60000,
        )
