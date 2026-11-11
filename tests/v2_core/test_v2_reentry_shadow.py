from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from test_v2_directional_replay import SCOPE
from test_v2_reentry import assess, bar, history

from services.v2_reentry_shadow import ShadowOpportunityBook
from tests.support.v2_admission import database as database_fixture

database = database_fixture


def proof():
    return assess(
        history(bar(25, h="102", c="101.5", v="20")), previous_episode=str(uuid4())
    )


def test_concurrent_sources_only_one_shadow_claim_and_no_orders(database):
    book = ShadowOpportunityBook(database, SCOPE)
    assessment = proof()

    def claim(_):
        return book.claim(
            target="BTCUSDT", decision_id=str(uuid4()), assessment=assessment
        )["status"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(claim, range(4)))
    assert outcomes.count("RECORDED") == 1
    assert outcomes.count("DUPLICATE_OPPORTUNITY") == 3
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT count(*) FROM v2_state_history WHERE reason='REENTRY_SHADOW_CLAIM'"
            ).fetchone()[0]
            == 1
        )


def test_restart_retry_and_config_change_cannot_duplicate_structure(database):
    decision, assessment = str(uuid4()), proof()
    args = {"target": "BTCUSDT", "decision_id": decision, "assessment": assessment}
    assert ShadowOpportunityBook(database, SCOPE).claim(**args)["status"] == "RECORDED"
    assert (
        ShadowOpportunityBook(database, SCOPE).claim(**args)["status"]
        == "ALREADY_RECORDED"
    )
    changed = deepcopy(assessment)
    changed["policy_digest"] = "another-policy"
    assert (
        ShadowOpportunityBook(database, SCOPE).claim(
            **{**args, "decision_id": str(uuid4()), "assessment": changed}
        )["status"]
        == "DUPLICATE_OPPORTUNITY"
    )
    with pytest.raises(ValueError, match="RETRY_CONFLICT"):
        ShadowOpportunityBook(database, SCOPE).claim(**{**args, "assessment": changed})


def test_scope_and_symbol_isolation(database):
    assessment = proof()
    for scope, target in (
        (SCOPE, "BTCUSDT"),
        (replace(SCOPE, account_id="another"), "BTCUSDT"),
        (replace(SCOPE, environment="LIVE"), "BTCUSDT"),
        (SCOPE, "ETHUSDT"),
    ):
        assert (
            ShadowOpportunityBook(database, scope).claim(
                target=target, decision_id=str(uuid4()), assessment=assessment
            )["status"]
            == "RECORDED"
        )


def test_wait_never_claims_or_authorizes(database):
    assessment = proof()
    assessment["status"] = "WAIT"
    assert ShadowOpportunityBook(database, SCOPE).claim(
        target="BTCUSDT", decision_id=str(uuid4()), assessment=assessment
    ) == {"status": "NOT_READY", "execution_authorized": False}
    with database() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM v2_state_history WHERE reason='REENTRY_SHADOW_CLAIM'"
            ).fetchone()[0]
            == 0
        )


def test_claim_rolls_back_with_owning_transaction(database):
    from contextlib import nullcontext

    assessment = proof()
    decision = str(uuid4())
    with database() as conn:
        assert (
            ShadowOpportunityBook(lambda: nullcontext(conn), SCOPE).claim(
                target="BTCUSDT", decision_id=decision, assessment=assessment
            )["status"]
            == "RECORDED"
        )
        conn.rollback()
    assert (
        ShadowOpportunityBook(database, SCOPE).claim(
            target="BTCUSDT", decision_id=decision, assessment=assessment
        )["status"]
        == "RECORDED"
    )
