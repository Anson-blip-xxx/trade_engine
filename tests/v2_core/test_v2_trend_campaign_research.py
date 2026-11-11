"""Fake ports plus explicitly isolated QA PG; never the business DB or a venue."""

import ast
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from test_v2_directional_cash import closed as closed_fixture

from services import v2_trend_campaign_research as worker
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope

HOUR = worker.HOUR
MINUTE = 60_000
NOW = 107 * HOUR
SCOPE = AccountScope("BINANCE", "qa-campaign-a", "SANDBOX", "FUTURES")
database = database_fixture
closed = closed_fixture


def case(identity="episode-a", *, net="22"):
    return {
        "trade": {
            "id": identity,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "opened_ms": 99 * HOUR + MINUTE,
            "closed_ms": 100 * HOUR + 30 * MINUTE,
            "source": "s3",
            "entry_mode": "TREND_BREAKOUT",
            "entry_profile": "causal-trend-entry-v1",
            "config_digest": "a" * 64,
            "strategy_version": "qa",
            "net_pnl": net,
            "fees_usdt": "1",
            "opening_quantity": "2",
            "closing_quantity": "2",
            "opening_notional": "200",
            "closing_notional": "223",
            "sizing": {"stop_price": "80"},
            "settlement_state": "SETTLED",
            "settlement_revision": 3,
            "settled_ms": 101 * HOUR,
            "maintenance": False,
            "exit_reason": "QA",
        },
        "fills": [
            {
                "identity": "o",
                "t": 99 * HOUR + MINUTE,
                "leg": "OPEN",
                "quantity": "2",
                "price": "100",
            },
            {
                "identity": "p",
                "t": 99 * HOUR + 40 * MINUTE,
                "leg": "CLOSE",
                "quantity": ".5",
                "price": "110",
            },
            {
                "identity": "c",
                "t": 100 * HOUR + 30 * MINUTE,
                "leg": "CLOSE",
                "quantity": "1.5",
                "price": "112",
            },
        ],
    }


class Archive:
    environment = "SANDBOX"

    def __init__(self, *, db=None):
        self.calls = []
        self.db = db

    def read(self, symbol, start, end):
        if self.db:
            assert self.db.active == 0, "PG snapshot must finish before archive I/O"
        self.calls.append((symbol, start, end))
        rows = []
        for t in range(start, end, MINUTE):
            p = Decimal(100) + Decimal((t - 100 * HOUR) // MINUTE) * Decimal(".005")
            rows.append(
                {
                    "t": t,
                    "o": str(p),
                    "h": str(p + Decimal(".01")),
                    "l": str(p - Decimal(".01")),
                    "c": str(p),
                    "v": "1",
                }
            )
        return list(reversed(rows))


class Rows:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class MemoryDB:
    """Small SQL recorder for default selection/persistence contract, not PG."""

    def __init__(self, cases=None):
        self.cases = cases or [case()]
        self.rows = {}
        self.sql = []
        self.active = 0

    def __call__(self):
        return self

    def __enter__(self):
        self.active += 1
        return self

    def __exit__(self, *_):
        self.active -= 1

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if sql.startswith("SET "):
            return Rows()
        if "INSERT INTO v2_trend_campaign_reports" in sql:
            report = json.loads(params[-2])
            key = (params[0], params[1], params[3], params[5])
            if key in self.rows:
                return Rows()
            self.rows[key] = (report, params[-1])
            return Rows([(report["report_digest"],)])
        if "SELECT DISTINCT ON(episode_digest,facts_digest)" in sql:
            selected = {}
            for report, at in self.rows.values():
                if (
                    report["method_version"],
                    report["policy_digest"],
                    report["scope_digest"],
                ) != tuple(params[:3]):
                    continue
                key = (report["episode_digest"], report["facts_digest"])
                if key not in selected or at >= selected[key][-1]:
                    selected[key] = (*key, report["status"], report["ready_at_ms"], at)
            return Rows(selected.values())
        if "SELECT i.intent_id::text" in sql:
            return Rows(
                [
                    (c["trade"]["id"], c["trade"].get("entry_profile"))
                    for c in self.cases
                ]
            )
        if "SELECT f.fill_key" in sql:
            c = next(c for c in self.cases if c["trade"]["id"] == params[0])
            return Rows(
                [
                    tuple(f[k] for k in ("identity", "t", "leg", "quantity", "price"))
                    for f in c["fills"]
                ]
            )
        raise AssertionError(f"unexpected SQL: {sql}")


def fake_review(monkeypatch, db):
    from services import v2_profit_capture_review

    monkeypatch.setattr(
        v2_profit_capture_review,
        "strict_trade_review",
        lambda *a, **k: {
            "trades": deepcopy([c["trade"] for c in db.cases]),
            "truncated": False,
        },
    )


def scopes(*_, **__):
    return [SCOPE]


def evaluate(c=None, *, now=NOW, read=None, policy=None):
    return worker.evaluate(
        c or case(),
        SCOPE,
        read or Archive().read,
        now_ms=now,
        policy=policy or worker.CampaignPolicy(),
    )


def test_mature_full_flow_forks_only_real_residual_and_keeps_funding_unknown():
    c = case()
    before = deepcopy(c)
    result = evaluate(c)
    assert c == before
    assert result["status"] == "ANALYZED"
    assert result["observation_end_ms"] == 106 * HOUR
    assert result["actual_baseline"]["net_pnl_usdt"] == "22"
    assert result["actual_baseline"]["gross_pnl_usdt"] == "23"
    shadow = result["residual_shadow"]
    assert shadow["residual_quantity"] == "1.5"
    assert shadow["timeliness"] == "NOT_PROVEN"
    assert shadow["modeled_net_delta_with_reported_funding"] is None
    assert all(a["reported_funding_cashflow"] is None for a in shadow["arms"].values())
    assert all(
        a["modeled_net_with_reported_funding"] is None for a in shadow["arms"].values()
    )
    assert result["net_pnl_delta_vs_actual"] is None
    assert result["entry_profile"] == "causal-trend-entry-v1"
    assert result["execution_authorized"] is result["promotion_authorized"] is False
    assert result["market_source"] == "BINANCE_USDM_SANDBOX"
    assert "qa-campaign-a" not in json.dumps(result) and "episode-a" not in json.dumps(
        result
    )


def test_immature_is_pending_without_archive_io_and_not_zero_return():
    archive = Archive()
    result = evaluate(now=105 * HOUR, read=archive.read)
    assert result["status"] == "PENDING" and result["residual_shadow"] is None
    assert result["ready_at_ms"] == 106 * HOUR + 300_000
    assert archive.calls == []
    assert result["net_pnl_delta_vs_actual"] is None


def test_already_flat_at_boundary_cannot_be_resurrected():
    c = case()
    c["fills"][-1]["t"] = c["trade"]["closed_ms"] = 100 * HOUR
    archive = Archive()
    result = evaluate(c, read=archive.read)
    assert result["status"] == "NOT_APPLICABLE"
    assert result["reason"] == "NO_ACTUAL_RESIDUAL_AT_ACTIVATION" and not archive.calls


def test_actual_trade_can_close_after_fixed_shadow_horizon_without_leaking_path():
    c = case()
    c["fills"][-1]["t"] = c["trade"]["closed_ms"] = 110 * HOUR
    archive = Archive()
    result = evaluate(c, read=archive.read, now=112 * HOUR)
    assert result["status"] == "ANALYZED"
    assert archive.calls[-1][2] == 106 * HOUR
    assert result["actual_baseline"]["closed_ms"] > result["observation_end_ms"]


@pytest.mark.parametrize(
    "field,value", [("opening_quantity", "3"), ("closing_notional", "999")]
)
def test_invalid_fill_totals_never_read_market_or_claim_success(field, value):
    c = case()
    c["trade"][field] = value
    archive = Archive()
    result = evaluate(c, read=archive.read)
    assert result["status"] == "UNKNOWN" and result["actual_baseline"] is None
    assert not archive.calls


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows[1:],
        lambda rows: [{**r, "environment": "LIVE"} for r in rows],
        lambda rows: [{**r, "market_source": "BINANCE_USDM_LIVE"} for r in rows],
    ],
)
def test_gaps_and_live_market_evidence_are_never_accepted(change):
    result = evaluate(read=lambda *args: change(Archive().read(*args)))
    assert result["status"] == "UNKNOWN"
    assert result["residual_shadow"] is None


def test_private_error_is_sanitized():
    def broken(*_):
        raise RuntimeError("DSN password=do-not-disclose")

    result = evaluate(read=broken)
    assert result["status"] == "UNKNOWN"
    assert "do-not-disclose" not in json.dumps(result)


def test_default_read_selection_all_outcomes_idempotent_and_snapshot_closed(
    monkeypatch,
):
    db = MemoryDB([case("win"), case("loss", net="-10")])
    fake_review(monkeypatch, db)
    result = worker.run(db, Archive(db=db), now_ms=NOW, scope_reader=scopes)
    assert result["status_counts"] == {"ANALYZED": 2}
    assert len(db.rows) == 2
    assert {r[0]["actual_baseline"]["net_pnl_usdt"] for r in db.rows.values()} == {
        "22",
        "-10",
    }
    second = worker.run(db, Archive(db=db), now_ms=NOW + 1, scope_reader=scopes)
    assert not second["outcomes"] and second["archive_calls"] == 0
    assert len(db.rows) == 2
    writes = [q for q, _ in db.sql if q.lstrip().split()[0] not in {"SET", "SELECT"}]
    assert writes and all(
        q.startswith("INSERT INTO v2_trend_campaign_reports") for q in writes
    )


def test_pending_then_mature_appends_without_overwriting(monkeypatch):
    db = MemoryDB()
    fake_review(monkeypatch, db)
    one = worker.run(db, Archive(db=db), now_ms=105 * HOUR, scope_reader=scopes)
    assert one["status_counts"] == {"PENDING": 1} and len(db.rows) == 1
    original = deepcopy(db.rows)
    two = worker.run(db, Archive(db=db), now_ms=105 * HOUR + 1, scope_reader=scopes)
    assert two["outcomes"] == [] and len(db.rows) == 1
    three = worker.run(db, Archive(db=db), now_ms=NOW, scope_reader=scopes)
    assert three["status_counts"] == {"ANALYZED": 1} and len(db.rows) == 2
    assert all(db.rows[k] == v for k, v in original.items())


def test_changed_settlement_revision_is_new_fact_and_old_report_is_retained(
    monkeypatch,
):
    db = MemoryDB()
    fake_review(monkeypatch, db)
    worker.run(db, Archive(), now_ms=NOW, scope_reader=scopes)
    original = deepcopy(db.rows)
    db.cases[0]["trade"].update(settlement_revision=4, net_pnl="21.5")
    result = worker.run(db, Archive(), now_ms=NOW + 1, scope_reader=scopes)
    assert result["status_counts"] == {"ANALYZED": 1} and len(db.rows) == 2
    assert all(db.rows[k] == v for k, v in original.items())


def test_missing_profile_cannot_be_attributed_to_new_entry():
    c = case()
    c["trade"].pop("entry_profile")
    assert evaluate(c)["entry_profile"] == "UNRECORDED_LEGACY_OR_UNKNOWN"


def test_scope_failure_does_not_stop_other_account_and_no_private_error():
    b = AccountScope("BINANCE", "qa-campaign-b", "SANDBOX", "FUTURES")
    saved = []

    def reader(_connect, scope, **_):
        if scope == SCOPE:
            raise RuntimeError("secret-connection")
        return [case()]

    result = worker.run(
        None,
        Archive(),
        now_ms=NOW,
        scope_reader=lambda *a, **k: [SCOPE, b],
        case_reader=reader,
        report_writer=lambda c, r, **k: saved.append(r) or "INSERTED",
    )
    assert result["status"] == "PARTIAL" and len(saved) == 1
    assert result["errors"][0]["stage"] == "CASES"
    assert "secret-connection" not in json.dumps(result)


def test_bad_case_and_write_failure_do_not_stop_next_case():
    calls = []

    def write(_, report, **__):
        calls.append(report)
        if len(calls) == 1:
            raise RuntimeError("secret-password")
        return "INSERTED"

    result = worker.run(
        None,
        Archive(),
        now_ms=NOW,
        scope_reader=scopes,
        case_reader=lambda *a, **k: [{}, case("a"), case("b")],
        report_writer=write,
    )
    assert len(result["outcomes"]) == 3 and len(calls) == 2
    assert result["outcomes"][-1]["persistence"] == "INSERTED"
    assert result["status"] == "PARTIAL" and "secret-password" not in json.dumps(result)


def test_global_episode_cap_and_account_rotation():
    saved, inspected = [], []
    b = AccountScope("BINANCE", "qa-campaign-b", "SANDBOX", "FUTURES")

    def reader(_, scope, *, limit, **__):
        inspected.append(scope)
        return [case(str(i)) for i in range(limit)]

    policy = worker.CampaignPolicy(max_episodes=2)
    common = {
        "policy": policy,
        "scope_reader": lambda *a, **k: [SCOPE, b],
        "case_reader": reader,
        "report_writer": lambda _, r, **k: saved.append(r) or "INSERTED",
    }
    worker.run(None, Archive(), now_ms=NOW, **common)
    worker.run(None, Archive(), now_ms=NOW + HOUR, **common)
    assert len(saved) == 4 and inspected == [b, SCOPE]


def test_archive_budget_counts_failures_and_never_falls_back_to_network():
    archive = Archive()
    policy = worker.CampaignPolicy(max_archive_minutes=1440)
    saved = []
    result = worker.run(
        None,
        archive,
        now_ms=NOW,
        policy=policy,
        scope_reader=scopes,
        case_reader=lambda *a, **k: [case("a"), case("b")],
        report_writer=lambda _, r, **k: saved.append(r) or "INSERTED",
    )
    assert result["status_counts"] == {"UNKNOWN": 2}
    assert archive.calls == []
    assert all(r["reason"] == "PROFIT_AUDIT_ARCHIVE_BUDGET" for r in saved)


def test_pending_report_persist_is_idempotent_and_rejects_tampering():
    report = evaluate(now=105 * HOUR)
    db = MemoryDB()
    assert worker.persist(db, report, now_ms=NOW) == "INSERTED"
    assert worker.persist(db, report, now_ms=NOW + 1) == "EXISTING"
    assert len(db.rows) == 1
    with pytest.raises(ValueError, match="DIGEST_MISMATCH"):
        worker.persist(db, {**report, "symbol": "ETHUSDT"}, now_ms=NOW)
    with pytest.raises(ValueError, match="PERSIST_SCOPE"):
        worker.persist(db, {**report, "execution_authorized": True}, now_ms=NOW)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_episodes": 11},
        {"max_scopes": 11},
        {"forward_hours": 25},
        {"forward_hours": True},
        {"lookback_days": 2},
        {"history_hours": 23},
    ],
)
def test_policy_bounds(kwargs):
    with pytest.raises(ValueError):
        worker.CampaignPolicy(**kwargs)


def test_policy_changes_are_separate_research_versions():
    assert worker.CampaignPolicy().forward_hours == 6
    assert (
        worker.CampaignPolicy().fingerprint
        != worker.CampaignPolicy(forward_hours=12).fingerprint
    )


def test_live_scope_and_unlabelled_or_live_archive_are_rejected():
    live = AccountScope("BINANCE", "qa-campaign-live", "LIVE", "FUTURES")
    result = worker.run(
        None, Archive(), now_ms=NOW, scope_reader=lambda *a, **k: [live]
    )
    assert result["status"] == "UNAVAILABLE"
    for label in (None, "LIVE"):
        archive = Archive()
        archive.environment = label
        with pytest.raises(ValueError, match="ARCHIVE_SANDBOX_ONLY"):
            worker.run(None, archive, now_ms=NOW, scope_reader=scopes)


def test_module_has_no_venue_api_or_live_entry_import_and_migration_is_append_only():
    text = Path(worker.__file__).read_text()
    tree = ast.parse(text)
    modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any("binance" in m.lower() or "testnet_entry" in m for m in modules)
    assert all(
        word not in text for word in ("create_order(", "cancel_order(", "leverage(")
    )
    root = Path(worker.__file__).resolve().parents[1]
    sql = (root / "db/migrations/20261031_trend_campaign_research.sql").read_text()
    assert "BEFORE UPDATE OR DELETE" in sql and "REPORTS_APPEND_ONLY" in sql
    assert "report ?& ARRAY" in sql and "FROM PUBLIC" in sql


@pytest.fixture
def campaign_database(database):
    root = Path(worker.__file__).resolve().parents[1]
    migration = (
        root / "db/migrations/20261031_trend_campaign_research.sql"
    ).read_text()
    with database() as conn:
        conn.execute(migration)
        conn.execute(migration)  # Schema installation itself is idempotent.
    return database


def test_isolated_pg_real_migration_idempotent_persist_and_append_only(
    campaign_database,
):
    import psycopg

    report = evaluate(now=105 * HOUR)
    assert worker.persist(campaign_database, report, now_ms=NOW) == "INSERTED"
    assert worker.persist(campaign_database, report, now_ms=NOW + 1) == "EXISTING"
    with campaign_database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_trend_campaign_reports").fetchone()[0]
            == 1
        )
        stored = conn.execute(
            "SELECT report FROM v2_trend_campaign_reports"
        ).fetchone()[0]
        assert stored == report
    for sql in (
        "UPDATE v2_trend_campaign_reports SET status='UNKNOWN'",
        "DELETE FROM v2_trend_campaign_reports",
    ):
        with (
            pytest.raises(psycopg.errors.RaiseException, match="APPEND_ONLY"),
            campaign_database() as conn,
        ):
            conn.execute(sql)
    # Even direct SQL cannot bypass the mandatory false-authority envelope.
    bad = deepcopy(report)
    bad["input_digest"] = "f" * 64
    bad["execution_authorized"] = None
    with pytest.raises(psycopg.errors.CheckViolation), campaign_database() as conn:
        conn.execute(
            """INSERT INTO v2_trend_campaign_reports
                (method_version,policy_digest,scope_digest,episode_digest,facts_digest,
                 input_digest,report_digest,status,ready_at_ms,report)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                worker.VERSION,
                bad["policy_digest"],
                bad["scope_digest"],
                bad["episode_digest"],
                bad["facts_digest"],
                bad["input_digest"],
                bad["report_digest"],
                bad["status"],
                bad["ready_at_ms"],
                json.dumps(bad),
            ),
        )


def test_isolated_pg_default_scope_case_read_current_settlement_and_no_ledger_writes(
    campaign_database, closed
):
    import time
    from dataclasses import replace

    from test_v2_directional_replay import SCOPE as DB_SCOPE
    from test_v2_directional_settlement import baseline, service

    runtime, episode, income = closed
    baseline(campaign_database, runtime, episode)
    assert service(campaign_database, income).run_once()["status"] == "CLEAR"
    now = time.time_ns() // 1_000_000 + 1
    policy = worker.CampaignPolicy()
    scopes_found = worker.load_scopes(campaign_database, now_ms=now, policy=policy)
    assert scopes_found == [DB_SCOPE]
    cases = worker.load_cases(
        campaign_database, DB_SCOPE, now_ms=now, policy=policy, limit=5
    )
    assert len(cases) == 1 and cases[0]["trade"]["settlement_state"] == "SETTLED"
    assert cases[0]["trade"]["net_pnl"] is not None
    assert (
        worker.load_cases(
            campaign_database,
            DB_SCOPE,
            now_ms=cases[0]["trade"]["settled_ms"] - 1,
            policy=policy,
            limit=5,
        )
        == []
    )
    assert (
        worker.load_cases(
            campaign_database,
            replace(DB_SCOPE, account_id="other"),
            now_ms=now,
            policy=policy,
            limit=5,
        )
        == []
    )
    counts = "SELECT (SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_fills),(SELECT count(*) FROM v2_state_history),(SELECT count(*) FROM v2_domain_outbox)"
    with campaign_database() as conn:
        before = conn.execute(counts).fetchone()
    result = worker.run(campaign_database, Archive(), now_ms=now)
    # The existing ledger fixture has fills at epoch 3 / 10 ms, already flat
    # before the first hour: a real load must never invent a residual position.
    assert result["status_counts"] == {"NOT_APPLICABLE": 1}
    assert worker.run(campaign_database, Archive(), now_ms=now + 1)["outcomes"] == []
    with campaign_database() as conn:
        assert conn.execute(counts).fetchone() == before
    runtime.data.ledger.adjustment(
        episode_id=episode,
        source_id="qa-campaign-late-funding",
        amount_text="-0.02",
        currency="USDT",
        kind="FUNDING",
        occurred_at_ms=70001,
        evidence={"source": "qa"},
    )
    assert (
        worker.load_cases(
            campaign_database,
            DB_SCOPE,
            now_ms=time.time_ns() // 1_000_000 + 1,
            policy=policy,
            limit=5,
        )
        == []
    )


def test_isolated_pg_least_privilege_role_has_no_trading_write_authority(
    campaign_database, closed
):
    import time
    from contextlib import contextmanager
    from uuid import uuid4

    import psycopg
    from psycopg import sql
    from test_v2_directional_settlement import baseline, service

    runtime, episode, income = closed
    baseline(campaign_database, runtime, episode)
    assert service(campaign_database, income).run_once()["status"] == "CLEAR"
    name = "campaign_research_" + uuid4().hex
    tables = [
        "v2_trade_intents",
        "v2_orders",
        "v2_fills",
        "v2_decision_evidence",
        "v2_settlements",
        "v2_directional_followups",
        "v2_strategy_decisions",
        "v2_episodes",
    ]
    with campaign_database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(name)
            )
        )
        for table in tables:
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(name)
                )
            )
        conn.execute(
            sql.SQL("GRANT SELECT,INSERT ON v2_trend_campaign_reports TO {}").format(
                sql.Identifier(name)
            )
        )

    @contextmanager
    def restricted():
        with campaign_database() as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(name)))
            yield conn

    try:
        result = worker.run(
            restricted, Archive(), now_ms=time.time_ns() // 1_000_000 + 1
        )
        assert (
            result["status_counts"] == {"NOT_APPLICABLE": 1} and result["errors"] == []
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), restricted() as conn:
            conn.execute("UPDATE v2_orders SET quantity=quantity WHERE false")
    finally:
        with campaign_database() as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
