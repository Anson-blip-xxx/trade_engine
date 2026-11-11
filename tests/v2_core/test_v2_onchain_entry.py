"""Faults in this optional sidecar never call a trading recovery path."""

import pytest

from services.v2_onchain_entry import GIB, run_batch

NOW = 1793434200000


class Store:
    def __init__(self, symbols=("QUSDT", "AKEUSDT"), *, mapped=True):
        self.symbols, self.mapped = symbols, mapped
        self.saved, self.runs, self.circuits = [], [], []
        self.retry_at = 0

    def prune(self, now_ms, limit):
        assert limit == 200
        return {"observations": 0, "runs": 0}

    def read_circuit(self, provider, now_ms):
        assert provider == "DEXPAPRIKA"
        return {"retry_after_ms": self.retry_at}

    def select_symbols(self, now_ms, limit):
        return [
            {"symbol": s, "membership": ["ACTIVE_SANDBOX_POSITION"]}
            for s in self.symbols[:limit]
        ]

    def lookup_mapping(self, target, now_ms):
        return (
            {
                "symbol": target,
                "network_id": "bsc",
                "token_address": "0x" + "1" * 40,
                "mapping_id": "public-map",
            }
            if self.mapped
            else None
        )

    def save(self, bundle, **kwargs):
        self.saved.append((bundle, kwargs))
        return {"inserted": True}

    def record_circuit(self, provider, **kwargs):
        self.circuits.append(kwargs)
        self.retry_at = kwargs.get("retry_after_ms", 0)
        return {"retry_after_ms": self.retry_at}

    def record_run(self, **kwargs):
        self.runs.append(kwargs)


class Market:
    halt_reason = None
    retry_after_seconds = None

    def __init__(self):
        self.calls = []

    def collect(self, network_id, token_address, *, now_ms):
        self.calls.append((network_id, token_address))
        return {
            "started_at_ms": NOW + 1,
            "finished_at_ms": NOW + 2,
            "components": {
                "token": {"status": "OBSERVED"},
                "pools": {"status": "OBSERVED"},
            },
        }


def describe(bundle, mapping, **kwargs):
    assert kwargs["as_of_ms"] == NOW + 2
    return {"status": "OBSERVED_AGGREGATE", "execution_authorized": False}


def run(store, market=None, **kwargs):
    return run_batch(
        store,
        lambda: market or Market(),
        now_ms=NOW,
        clock_ms=lambda: NOW + 3,
        free_disk_bytes=20 * GIB,
        describe=describe,
        **kwargs,
    )


def fail(*args, **kwargs):
    raise RuntimeError("private raw server body must not be printed")


def test_success_is_only_a_research_write():
    store, market = Store(), Market()
    result = run(store, market)
    assert result["status"] == "COMPLETE" and result["inserted"] == 2
    assert len(market.calls) == len(store.circuits) == 2
    assert result["execution_authorized"] is result["trading_dependency"] is False
    assert all(b[1]["slot_ms"] == NOW for b in store.saved)


def test_no_positions_does_not_invent_watchlist_or_open_network():
    store = Store(())
    result = run_batch(store, fail, now_ms=NOW, clock_ms=lambda: NOW, describe=describe)
    assert result["status"] == "IDLE" and result["attempted"] == 0


def test_unmapped_symbols_never_trigger_fuzzy_lookup_or_network():
    store = Store(mapped=False)
    result = run_batch(store, fail, now_ms=NOW, clock_ms=lambda: NOW, describe=describe)
    assert result["status"] == "PARTIAL" and result["attempted"] == 0
    assert result["outcome_counts"] == {"UNKNOWN": 2}


def test_persistent_cooldown_survives_new_worker_instance():
    store = Store()
    store.retry_at = NOW + 300_000
    result = run_batch(store, fail, now_ms=NOW, clock_ms=lambda: NOW, describe=describe)
    assert result["status"] == "COOLDOWN" and result["selected"] == 0


@pytest.mark.parametrize(
    "method", ["read_circuit", "select_symbols", "prune", "lookup_mapping"]
)
def test_local_state_failures_prevent_provider_requests(method):
    store = Store()
    setattr(store, method, fail)
    result = run_batch(store, fail, now_ms=NOW, clock_ms=lambda: NOW, describe=describe)
    assert result["status"] == "DEFERRED" and result["attempted"] == 0
    assert "private" not in str(result)


def test_http_rate_limit_persists_cooldown_and_stops_other_symbols():
    store, market = Store(), Market()
    market.halt_reason, market.retry_after_seconds = "ONCHAIN_HTTP_RATE_LIMITED", 600
    result = run(store, market)
    assert result["status"] == "DEFERRED" and len(market.calls) == 1
    assert store.retry_at == NOW + 3 + 600_000
    assert store.circuits[-1]["outcome"] == "FAILURE"


@pytest.mark.parametrize("method", ["save", "record_circuit"])
def test_failed_write_stops_more_external_work(method):
    store, market = Store(), Market()
    setattr(store, method, fail)
    result = run(store, market)
    assert result["status"] == "DEFERRED" and len(market.calls) == 1


def test_low_disk_skips_provider():
    result = run_batch(
        Store(),
        fail,
        now_ms=NOW,
        clock_ms=lambda: NOW,
        free_disk_bytes=GIB,
        describe=describe,
    )
    assert result["status"] == "DEFERRED" and result["attempted"] == 0
    assert result["errors"] == {"ONCHAIN_DISK_RESERVE": 1}


def test_explicit_smoke_is_separate_from_actual_positions():
    store = Store(())
    result = run(store, research_symbols=("QUSDT",))
    assert result["scope"] == "EXPLICIT_RESEARCH_SMOKE" and result["inserted"] == 1
    assert store.saved[0][1]["membership"] == ["EXPLICIT_RESEARCH_SMOKE"]


def test_unknown_is_not_a_complete_result():
    result = run_batch(
        Store(),
        Market,
        now_ms=NOW,
        clock_ms=lambda: NOW + 3,
        describe=lambda *a, **k: {"status": "UNKNOWN"},
    )
    assert result["status"] == "PARTIAL"


def test_run_write_failure_is_visible_in_journal_result():
    store = Store()
    store.record_run = fail
    result = run(store)
    assert result["status"] == "DEFERRED"
    assert result["errors"]["ONCHAIN_RUN_RECORD_UNAVAILABLE"] == 1
