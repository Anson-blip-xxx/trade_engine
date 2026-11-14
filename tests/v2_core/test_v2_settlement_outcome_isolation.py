"""Post-settlement analysis failures preserve committed financial facts."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from services import v2_directional_settlement as module
from v2_core.account_risk import AccountScope

EPISODE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def stage_case(monkeypatch):
    """Inject only storage/commit boundaries; exercise actual stage orchestration."""
    state = SimpleNamespace(
        episodes={EPISODE: "CLOSED"},
        recorded=set(),
        outcome_failures=set(),
        outcome_calls=[],
        cash_calls=[],
        commits=[],
        cash_error=None,
        settlement_error=None,
        read_error=None,
    )

    class Connection:
        def execute(self, statement, params):
            if state.read_error is not None:
                raise state.read_error
            if "AND e.status='SETTLED'" in statement:
                values = [
                    (episode,)
                    for episode, status in state.episodes.items()
                    if status == "SETTLED" and episode not in state.recorded
                ]
                return SimpleNamespace(fetchall=lambda: values)
            if "AND e.status<>'SETTLED'" in statement:
                values = [
                    (episode,)
                    for episode, status in state.episodes.items()
                    if status != "SETTLED"
                ]
                return SimpleNamespace(fetchall=lambda: values)
            if "SELECT 1 FROM v2_orders" in statement:
                return SimpleNamespace(fetchone=lambda: None)
            pytest.fail("unexpected storage operation")

    def record(episode):
        state.outcome_calls.append(episode)
        assert state.episodes[episode] == "SETTLED"
        if episode in state.outcome_failures:
            raise ValueError("DIRECTIONAL_DECISION_FACT_MISMATCH")
        state.recorded.add(episode)
        return {"episode": episode, "net_pnl": "-1"}

    def audit(episode):
        state.cash_calls.append(episode)
        if state.cash_error is not None:
            raise state.cash_error
        return {"status": "CASH_MATCHED"}

    def settle_locked(episode, cash):
        if state.settlement_error is not None:
            raise state.settlement_error
        state.commits.append(episode)
        state.episodes[episode] = "SETTLED"
        return {
            "status": "SETTLED",
            "episode": episode,
            "net_pnl": "-1",
            "settlement_authorized": False,
        }

    stage = object.__new__(module.DirectionalSettlementStage)
    stage.connect = lambda: nullcontext(Connection())
    stage.scope = AccountScope("BINANCE", "qa-outcome-isolation", "SANDBOX", "FUTURES")
    stage.limit = 10
    stage.outcomes = SimpleNamespace(record=record)
    stage.cash = SimpleNamespace(audit=audit)
    stage._settle_locked = settle_locked
    monkeypatch.setattr(module, "session_advisory_lock", lambda *_: nullcontext(True))
    return stage, state


def test_direct_settle_preserves_committed_status_when_analysis_fails(stage_case):
    stage, state = stage_case
    state.outcome_failures.add(EPISODE)

    result = stage.settle(EPISODE)

    assert result["status"] == "SETTLED"
    assert result["net_pnl"] == "-1"
    assert result["outcome"] == {
        "status": "BLOCKED",
        "error_code": "DIRECTIONAL_DECISION_FACT_MISMATCH",
    }
    assert state.commits == [EPISODE]


def test_failed_outcome_is_visible_and_retried_without_repeating_settlement(stage_case):
    stage, state = stage_case
    state.outcome_failures.add(EPISODE)

    result = stage.run_once()

    assert result["status"] == "CLEAR"
    assert result["settlements"][EPISODE]["status"] == "SETTLED"
    assert "outcome" not in result["settlements"][EPISODE]
    assert result["outcomes"][EPISODE] == {
        "status": "BLOCKED",
        "error_code": "DIRECTIONAL_DECISION_FACT_MISMATCH",
    }
    assert result["settlement_authorized"] is False

    state.outcome_failures.clear()
    retried = stage.run_once()

    assert retried["status"] == "CLEAR"
    assert retried["settlements"] == {}
    assert retried["outcomes"][EPISODE]["net_pnl"] == "-1"
    assert state.outcome_calls == [EPISODE, EPISODE]
    assert state.commits == state.cash_calls == [EPISODE]
    assert stage.run_once() == {
        "status": "CLEAR",
        "settlements": {},
        "outcomes": {},
        "settlement_authorized": False,
    }


def test_previously_settled_analysis_failure_does_not_block_financial_stage(stage_case):
    stage, state = stage_case
    state.episodes[EPISODE] = "SETTLED"
    state.outcome_failures.add(EPISODE)

    result = stage.run_once()

    assert result["status"] == "CLEAR"
    assert result["settlements"] == {}
    assert result["outcomes"][EPISODE]["status"] == "BLOCKED"
    assert not state.cash_calls and not state.commits


@pytest.mark.parametrize("boundary", ["cash_error", "settlement_error"])
def test_financial_failure_still_blocks_without_attempting_analysis(stage_case, boundary):
    stage, state = stage_case
    setattr(state, boundary, ValueError("WALLET_CASH_MISMATCH"))

    result = stage.run_once()

    assert result["status"] == "BLOCKED"
    assert result["settlements"][EPISODE] == {
        "status": "BLOCKED",
        "error_code": "WALLET_CASH_MISMATCH",
    }
    assert result["outcomes"] == {}
    assert state.episodes[EPISODE] == "CLOSED"
    assert not state.outcome_calls and not state.commits


def test_analysis_isolation_never_masks_another_episodes_cash_failure(stage_case):
    stage, state = stage_case
    state.episodes[OTHER] = "SETTLED"
    state.outcome_failures.add(OTHER)
    state.cash_error = ValueError("INCOME_FILL_MISMATCH")

    result = stage.run_once()

    assert result["status"] == "BLOCKED"
    assert result["settlements"][EPISODE]["error_code"] == "INCOME_FILL_MISMATCH"
    assert result["outcomes"][OTHER]["error_code"] == "DIRECTIONAL_DECISION_FACT_MISMATCH"
    assert not state.commits


def test_unknown_pending_scan_does_not_claim_financial_clearance(stage_case):
    stage, state = stage_case
    state.read_error = TimeoutError("storage unavailable")

    with pytest.raises(TimeoutError):
        stage.run_once()

    assert not state.cash_calls and not state.commits
