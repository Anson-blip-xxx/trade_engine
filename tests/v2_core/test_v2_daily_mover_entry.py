"""Entrypoint orchestration, injected ports only; no live DB/network/Telegram."""

import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import services.v2_daily_mover_entry as entry
from services.v2_daily_mover_market import MoverMarketError


class Harness:
    def __init__(self, monkeypatch):
        self.acquired = True
        self.events, self.statements, self.messages, self.failures = [], [], [], []
        self.job_calls, self.budgets = [], []
        self.job_options = []
        self.flow_reviewers = []
        self.flow_evaluators = []
        self.archive_closes = self.context_exits = self.unlocks = 0
        self.archive_created = 0
        self.job_error = self.close_error = self.unlock_error = self.notify_error = (
            self.config_error
        ) = None
        self.receipt = 123
        self.claims, self.delivery = {}, {}
        harness = self

        class Conn:
            def execute(self, sql):
                harness.statements.append(sql)
                if "pg_try_advisory_lock" in sql:
                    return SimpleNamespace(fetchone=lambda: (harness.acquired,))
                if "pg_advisory_unlock" in sql:
                    harness.unlocks += 1
                    if harness.unlock_error:
                        raise harness.unlock_error
                return SimpleNamespace(fetchone=lambda: None)

            def commit(self):
                pass

        @contextmanager
        def connect():
            try:
                yield Conn()
            finally:
                harness.context_exits += 1

        def connection_factory(dsn, *, schema):
            assert dsn == "fake-dsn-not-network"
            assert schema == "trade_v2"
            return connect

        class Archive:
            def __init__(self, *, factory):
                assert callable(factory)
                harness.archive_created += 1

            def read(self, *_):
                pytest.fail("fake job must not read actual candles")

            def close(self):
                harness.archive_closes += 1
                if harness.close_error:
                    raise harness.close_error

        class Store:
            def __init__(self, _connect):
                self.connect = _connect

            def save_failure(self, day, code):
                harness.failures.append((day, code))
                identity = "failure-" + day
                harness.claims.setdefault(identity, "PENDING")
                return identity

            def claim_delivery(self, identity):
                if harness.claims[identity] != "PENDING":
                    return False
                harness.claims[identity] = "SENDING"
                return True

            def delivered(self, identity, receipt=None):
                harness.delivery[identity] = receipt
                harness.claims[identity] = "SENT" if receipt else "UNKNOWN"

        def host_budget(_connect, *, environment, lane):
            harness.budgets.append((environment, lane))
            return object()

        class Job:
            def __init__(
                self,
                store,
                market,
                *,
                notifier,
                path_reader,
                force_evidence_refresh=False,
                flow_reviewer=None,
                flow_evaluator=None,
            ):
                assert isinstance(store, Store)
                assert market is not None
                assert callable(path_reader)
                assert callable(notifier)
                harness.job_options.append(force_evidence_refresh)
                harness.flow_reviewers.append(flow_reviewer)
                harness.flow_evaluators.append(flow_evaluator)

            def run(self, *, day):
                harness.job_calls.append(day)
                if harness.job_error:
                    raise harness.job_error
                return [{"status": "COMPLETE", "orders_submitted": False}]

        def make_notifier(values):
            if harness.config_error:
                raise harness.config_error
            assert values["V2_DAILY_MOVER_DSN"] == "fake-dsn-not-network"

            def notify(text):
                harness.messages.append(text)
                if harness.notify_error:
                    raise harness.notify_error
                return harness.receipt

            return notify

        monkeypatch.setattr(entry, "connection_factory", connection_factory)
        monkeypatch.setattr(entry, "ReentryArchive", Archive)
        monkeypatch.setattr(entry, "DailyMoverStore", Store)
        monkeypatch.setattr(entry, "HostBudget", host_budget)
        monkeypatch.setattr(entry, "MoverMarket", lambda budget: budget)
        monkeypatch.setattr(entry, "DailyMoverJob", Job)
        monkeypatch.setattr(entry, "make_notifier", make_notifier)
        monkeypatch.setattr(
            entry, "emit", lambda **fields: harness.events.append(fields)
        )

    def run(self, *, values=None, **kwargs):
        return entry.run(
            {"V2_DAILY_MOVER_DSN": "fake-dsn-not-network", **(values or {})}, **kwargs
        )


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)


def test_success_scopes_noncritical_public_budget_releases_research_lock(harness):
    assert harness.run(day="2026-10-25") == 0
    assert harness.job_calls == ["2026-10-25"]
    assert harness.budgets == [("LIVE", "market")]
    assert harness.archive_closes == harness.unlocks == harness.context_exits == 1
    assert any("statement_timeout='3s'" in sql for sql in harness.statements)
    assert any("lock_timeout='100ms'" in sql for sql in harness.statements)
    assert any("daily-mover-review-worker-v1" in sql for sql in harness.statements)
    assert all(
        "execution" not in sql and "trade_intents" not in sql
        for sql in harness.statements
    )
    assert not harness.messages


def test_duplicate_worker_cleans_resources_without_job_or_notification(harness):
    harness.acquired = False
    assert harness.run() == 0
    assert not harness.job_calls and not harness.budgets and not harness.messages
    assert harness.archive_created == harness.archive_closes == 0
    assert harness.context_exits == 1
    assert harness.unlocks == 0
    assert harness.events[-1]["status"] == "ALREADY_RUNNING"


def test_flow_opt_in_is_lazy_noncritical_and_disabled_by_default(harness):
    assert harness.run() == 0
    assert harness.flow_reviewers == [None]
    assert harness.flow_evaluators == [None]
    assert harness.run(values={"V2_MOVER_FLOW_ENABLED": "1"}) == 0
    reviewer = harness.flow_reviewers[-1]
    assert isinstance(reviewer, entry.DailyMoverFlowReview)
    assert isinstance(harness.flow_evaluators[-1], entry.FlowHistoryEvaluation)
    assert reviewer.market is None
    assert harness.budgets == [("LIVE", "market"), ("LIVE", "market")]


def test_invalid_optional_flow_setting_does_not_block_primary_report(harness):
    assert harness.run(values={"V2_MOVER_FLOW_ENABLED": "invalid-private-value"}) == 0
    assert harness.flow_reviewers == [None]
    assert any(e.get("status") == "DISABLED_INVALID_CONFIG" for e in harness.events)
    assert "invalid-private-value" not in str(harness.events)


def test_unexpected_job_failure_redacts_diagnostics_and_still_cleans_up(harness):
    harness.job_error = RuntimeError("private-url secret-key private-account")
    assert harness.run() == 1
    assert "private-url" not in str(harness.events)
    assert "secret-key" not in str(harness.messages)
    assert harness.failures[-1][1] == "MOVER_RESEARCH_JOB_FAILED"
    assert harness.archive_closes == harness.unlocks == harness.context_exits == 1
    assert harness.events[-1]["trading_affected"] is False


def test_known_public_quota_error_is_actionable_and_failure_dm_deduped(harness):
    harness.job_error = MoverMarketError("MOVER_SHARED_QUOTA_WAIT")
    assert harness.run() == 1
    assert harness.run() == 1
    assert harness.failures[-1][1] == "MOVER_SHARED_QUOTA_WAIT"
    assert len(harness.messages) == 1


def test_arbitrary_public_error_text_cannot_leak_into_digest_or_journal(harness):
    harness.job_error = MoverMarketError("SECRET_TOKEN private-endpoint")
    assert harness.run() == 1
    assert "SECRET_TOKEN" not in str(harness.events)
    assert "private-endpoint" not in str(harness.messages)


def test_manual_backfill_failure_digest_names_requested_day(harness):
    harness.job_error = MoverMarketError("MOVER_RATE_LIMITED")
    assert harness.run(day="2026-10-23") == 1
    assert harness.failures[-1][0] == "2026-10-23"
    assert "2026-10-23" in harness.messages[0]


def test_uncertain_failure_dm_is_not_blindly_retried(harness):
    harness.job_error = RuntimeError("internal")
    harness.notify_error = TimeoutError("could-be-delivered")
    assert harness.run() == 1
    assert harness.run() == 1
    assert len(harness.messages) == 1
    assert list(harness.claims.values()) == ["UNKNOWN"]


@pytest.mark.parametrize("receipt", [True, False, 0, -1, "123"])
def test_failure_digest_receipt_must_be_positive_integer(harness, receipt):
    harness.job_error = RuntimeError("internal")
    harness.receipt = receipt
    assert harness.run() == 1
    assert list(harness.claims.values()) == ["UNKNOWN"]


def test_notifier_config_failure_does_not_leak_archive_resource(harness):
    harness.config_error = ValueError("MOVER_PRIVATE_NOTIFICATION_CONFIG_INVALID")
    try:
        harness.run()
    except ValueError as error:
        assert str(error) == "MOVER_PRIVATE_NOTIFICATION_CONFIG_INVALID"
    assert harness.archive_created == harness.archive_closes == 0
    assert not harness.job_calls


def test_archive_cleanup_failure_cannot_prevent_unlock(harness):
    harness.close_error = RuntimeError("private-archive-endpoint")
    try:
        harness.run()
    except RuntimeError as error:
        assert "private-archive-endpoint" not in str(error)
    assert harness.unlocks == 1
    assert harness.context_exits == 1


def test_unlock_error_after_job_cannot_expose_database_error(harness):
    harness.unlock_error = RuntimeError("private-dsn password")
    try:
        harness.run()
    except RuntimeError as error:
        assert "private-dsn" not in str(error)
    assert harness.archive_closes == 1
    assert harness.context_exits == 1


class ProfileClient:
    def __init__(self, rows=None):
        self.rows = [[1, 1, 3, 20000, 134217728]] if rows is None else rows
        self.queries = []
        self.closed = False
        self.error = None

    def query(self, sql, **kwargs):
        self.queries.append((sql, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(result_rows=self.rows)

    def close(self):
        self.closed = True


def install_profile_client(monkeypatch, client):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect",
        SimpleNamespace(get_client=lambda **kwargs: calls.append(kwargs) or client),
    )
    return calls


def test_archive_client_uses_existing_restricted_profile_without_overriding_it(
    monkeypatch,
):
    client = ProfileClient()
    calls = install_profile_client(monkeypatch, client)
    result = entry.archive_client(
        host="127.0.0.1", port=18123, settings={"readonly": 1, "max_execution_time": 1}
    )
    assert result is client
    assert calls == [
        {
            "username": "tradev2shadow",
            "host": "127.0.0.1",
            "port": 18123,
        }
    ]
    assert len(client.queries) == 1
    sql, options = client.queries[0]
    assert sql.lstrip().upper().startswith("SELECT")
    assert not options.get("settings")
    for field in (
        "readonly",
        "max_threads",
        "max_execution_time",
        "max_result_rows",
        "max_memory_usage",
    ):
        assert field in sql
    assert not client.closed


@pytest.mark.parametrize(
    "caps",
    [
        [0, 1, 3, 20000, 134217728],
        [2, 1, 3, 20000, 134217728],
        [1, 0, 3, 20000, 134217728],
        [1, 2, 3, 20000, 134217728],
        [1, 1, 0, 20000, 134217728],
        [1, 1, 4, 20000, 134217728],
        [1, 1, 3, 0, 134217728],
        [1, 1, 3, 20001, 134217728],
        [1, 1, 3, 20000, 0],
        [1, 1, 3, 20000, 134217729],
    ],
)
def test_invalid_or_disabled_archive_profile_limits_are_rejected_and_client_closed(
    monkeypatch, caps
):
    client = ProfileClient([caps])
    calls = install_profile_client(monkeypatch, client)
    with pytest.raises((ValueError, RuntimeError)):
        entry.archive_client(host="127.0.0.1", port=18123, settings={"readonly": 0})
    assert "settings" not in calls[0]
    assert client.closed
    assert len(client.queries) == 1
    assert client.queries[0][0].lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize("rows", [[], [[1, 1]], [["invalid", 1, 3, 20000, 134217728]]])
def test_malformed_archive_profile_is_unknown_not_assumed_safe(monkeypatch, rows):
    client = ProfileClient(rows)
    install_profile_client(monkeypatch, client)
    with pytest.raises((ValueError, RuntimeError)):
        entry.archive_client(host="127.0.0.1", port=18123)
    assert client.closed


def test_archive_profile_query_error_is_sanitized_and_closes_client(monkeypatch):
    client = ProfileClient()
    client.error = RuntimeError("private-archive-endpoint private-password")
    install_profile_client(monkeypatch, client)
    with pytest.raises((ValueError, RuntimeError)) as caught:
        entry.archive_client(host="127.0.0.1", port=18123)
    assert "private-archive-endpoint" not in str(caught.value)
    assert "private-password" not in str(caught.value)
    assert client.closed
