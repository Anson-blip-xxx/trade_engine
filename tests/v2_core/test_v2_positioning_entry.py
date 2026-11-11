"""Optional research cannot submit orders, block trading, or hide failed slots."""

from services.v2_positioning_entry import GIB, run_batch

NOW = 1793434200000


class Store:
    def __init__(self, symbols=("QUSDT", "BTCUSDT")):
        self.symbols, self.saved, self.runs = symbols, [], []

    def select_symbols(self, now_ms, limit):
        return [
            {"symbol": s, "membership": ["FIXED_CONTROL"]} for s in self.symbols[:limit]
        ]

    def prune(self, now_ms, limit):
        assert limit == 500
        return 0

    def save(self, bundle, **kwargs):
        self.saved.append((bundle, kwargs))
        return {"inserted": True}

    def record_run(self, **kwargs):
        self.runs.append(kwargs)


class Market:
    halt_reason = None

    def __init__(self, fail=None, halt=None):
        self.calls, self.fail, self.halt = [], fail, halt

    def collect(self, symbol, now_ms):
        self.calls.append(symbol)
        if symbol == self.halt:
            self.halt_reason = "POSITIONING_RATE_LIMITED"
        if symbol == self.fail:
            raise ValueError("do not expose raw private details")
        return {"symbol": symbol, "started_at_ms": NOW, "finished_at_ms": NOW + 1}


def describe(bundle, **kwargs):
    assert kwargs == {"cutoff_ms": NOW + 1, "as_of_ms": NOW + 1}
    return {"execution_authorized": False, "components": {"oi": {"status": "OBSERVED"}}}


def run(store, market, **kwargs):
    return run_batch(
        store,
        market,
        now_ms=NOW,
        clock_ms=lambda: NOW + 2,
        free_disk_bytes=20 * GIB,
        describe=describe,
        **kwargs,
    )


def test_isolated_research_success_and_saved_slot():
    store, market = Store(), Market()
    report = run(store, market)
    assert report["status"] == "COMPLETE"
    assert report["inserted"] == report["selected"] == 2
    assert (
        report["execution_authorized"] is report["automatic_parameter_updates"] is False
    )
    assert all(item[1]["slot_ms"] == NOW for item in store.saved)
    assert len(store.runs) == 1


def test_bad_symbol_does_not_block_other_symbol_or_log_error_text():
    store, market = Store(), Market(fail="QUSDT")
    report = run(store, market)
    assert report["failed"] == 1 and report["inserted"] == 1
    assert market.calls == ["QUSDT", "BTCUSDT"]
    assert "private" not in str(report)


def test_host_quota_halt_stops_remaining_network_requests():
    report = run(Store(), Market(halt="QUSDT"))
    assert report["status"] == "DEFERRED"
    assert report["attempted"] == report["deferred_symbols"] == 1


def test_low_disk_skips_collection_but_runs_bounded_cleanup():
    store, market = Store(), Market()
    report = run_batch(
        store,
        market,
        now_ms=NOW,
        free_disk_bytes=GIB,
        clock_ms=lambda: NOW,
        describe=describe,
    )
    assert report["status"] == "DEFERRED" and not market.calls
    assert "POSITIONING_DISK_RESERVE" in report["errors"]


def test_unknown_component_is_reported_not_silently_complete():
    report = run_batch(
        Store(),
        Market(),
        now_ms=NOW,
        free_disk_bytes=20 * GIB,
        clock_ms=lambda: NOW + 2,
        describe=lambda *a, **k: {
            "components": {"oi": {"status": "UNKNOWN", "features": None}}
        },
    )
    assert report["status"] == "PARTIAL"
    assert report["unknown_components"] == {"oi": 2}
    assert report["failed"] == 0


def test_run_record_failure_is_observable_without_raising():
    store = Store()
    store.record_run = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("secret"))
    report = run(store, Market())
    assert report["status"] == "DEFERRED"
    assert "secret" not in str(report)


def test_no_catalog_selection_is_not_a_complete_success():
    report = run(Store(symbols=()), Market())
    assert report["status"] == "DEFERRED" and report["attempted"] == 0


def test_observed_component_with_incomplete_window_is_partial():
    report = run_batch(
        Store(),
        Market(),
        now_ms=NOW,
        free_disk_bytes=20 * GIB,
        clock_ms=lambda: NOW + 2,
        describe=lambda *a, **k: {
            "components": {
                "oi": {
                    "status": "OBSERVED",
                    "features": {
                        "window_3h": {"status": "OBSERVED"},
                        "window_6h": {"status": "UNKNOWN"},
                    },
                }
            }
        },
    )
    assert report["status"] == "PARTIAL"
    assert report["observed_components"] == {"oi": 2}
    assert report["unknown_windows"] == {"oi.window_6h": 2}
