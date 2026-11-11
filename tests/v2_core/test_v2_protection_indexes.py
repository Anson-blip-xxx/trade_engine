"""Protection lookups must not rescan unrelated historical business state."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.v2_admission import database as database_fixture
from v2_core.state import BusinessState, StateKey

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "namespace,index",
    [
        ("testnet-protection-v1", "v2_protection_active_states"),
        ("protection-recovery-result-v1", "v2_protection_recovery_keys"),
    ],
)
def test_protection_lookup_is_indexed_and_preserves_account_isolation(
    database, namespace, index
):
    key = StateKey("BINANCE", "qa-a", "SANDBOX", "FUTURES", namespace, "parent")
    with database() as c:
        store = BusinessState(lambda: nullcontext(c))
        for target in (key, replace(key, account_id="qa-b")):
            assert (
                store.change(
                    target,
                    expected_version=0,
                    request_key="init",
                    payload={"status": "TERMINAL_FLAT_LEDGER"},
                    reason="QA",
                ).code
                == "APPLIED"
            )
        # Also prove the partial index never makes tombstones visible.
        deleted = replace(key, key="deleted")
        store.change(
            deleted,
            expected_version=0,
            request_key="init",
            payload={},
            deleted=True,
            reason="QA",
        )
        c.execute("SET LOCAL enable_seqscan=off")
        query = """SELECT state_id::text FROM v2_business_state
            WHERE NOT deleted AND scope->>'namespace'=%s
            AND scope->>'key'=%s AND scope @> '{"account_id":"qa-a"}'::jsonb"""
        params = (namespace, "parent")
        assert c.execute(query, params).fetchall() == [(key.identity,)]
        plan = c.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone()[0]
        assert index in str(plan)
        assert c.execute(query, (namespace, "deleted")).fetchall() == []


def test_online_and_offline_indexes_match_and_migration_is_repeatable(database):
    migration = (ROOT / "db/migrations/20260928_protection_lookup.sql").read_text()
    online = (ROOT / "deploy/v2-testnet/protection-lookup-indexes.sql").read_text()

    def statements(text):
        return " ".join(
            line for line in text.splitlines() if not line.lstrip().startswith("--")
        ).split()

    assert statements(
        online.replace(" CONCURRENTLY", "").replace("trade_v2.", "")
    ) == statements(migration)
    with database() as c:
        c.execute(migration)
        c.execute(migration)
        assert (
            c.execute("""SELECT count(*) FROM pg_indexes
            WHERE schemaname=current_schema() AND indexname IN
            ('v2_protection_active_states','v2_protection_recovery_keys')""").fetchone()
            == (2,)
        )
