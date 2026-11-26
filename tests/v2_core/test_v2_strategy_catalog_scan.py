from contextlib import contextmanager
from threading import Event

import pytest

from services.v2_execution_guard import create_execution_guard
from services.v2_strategy_catalog_scan import _CLOCK, _WINDOW, _page, scan_catalog
from tests.v2_core.test_v2_strategy_candidate_producer import (
    database,
    fenced,
    now,
    scene,
)
from tests.v2_core.test_v2_strategy_entry_job import ROOT
from tests.v2_core.test_v2_strategy_prepare import at_database_time
from v2_core.shared_market import Catalog

__all__ = ["database", "fenced"]


def setup(fenced, symbols=("BTCUSDT", "ETHUSDT", "牛来USDT")):
    selected = scene(fenced)
    with fenced.admin() as conn:
        conn.execute(
            (ROOT / "db/migrations/20261126_strategy_scan_cursors.sql").read_text()
        )
    catalog = Catalog(fenced.admin, "SANDBOX", lambda: now(fenced.admin))
    row = catalog.read(60000)["BTCUSDT"]
    catalog.save({"symbols": [{**row, "symbol": symbol} for symbol in symbols]})
    return selected


def scan(fenced, selected, *, connect=None, **changes):
    return scan_catalog(
        fenced.admin if connect is None else connect,
        binding=selected.producer.binding,
        instance_id=selected.producer.instance_id,
        guard=create_execution_guard(
            fenced.admin, **selected.producer.binding.guard_args
        ),
        stop=Event(),
        **changes,
    )


@pytest.mark.parametrize("stride", [1, 2, 5])
@pytest.mark.parametrize("limit", [1, 2, 4])
def test_pure_unfinished_rotation_survives_arbitrary_window_gaps(stride, limit):
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT"]
    before, observed = None, []
    for index in range(10):
        window = (1 + index * stride) * _WINDOW
        selected, after, completed = _page(symbols, before, window, limit)
        observed.extend(selected)
        before = window, after, completed
        if completed:
            break
    assert observed == symbols
    selected, _, _ = _page(symbols, before, window + _WINDOW, limit)
    assert selected == symbols[:limit]
    assert _page(symbols, before, window, limit)[0] == []


def test_pure_partial_window_resumes_before_wrapping_and_clock_reversal_rejects():
    first = (2 * _WINDOW, "BBBUSDT", False)
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert _page(symbols, first, 3 * _WINDOW, 2) == (["CCCUSDT"], "CCCUSDT", True)
    with pytest.raises(ValueError, match="CLOCK_REVERSED"):
        _page(symbols, first, _WINDOW, 2)


def advance_catalog(fenced, tick):
    with fenced.admin() as conn:
        contracts = conn.execute(
            "SELECT contracts FROM v2_market_catalog WHERE environment='SANDBOX'"
        ).fetchone()[0]
    Catalog(fenced.admin, "SANDBOX", lambda: tick).save(
        {"symbols": list(contracts.values())}
    )
    return at_database_time(fenced.admin, tick)


def test_catalog_fair_pages_without_fixed_symbol_cap(fenced):
    selected = setup(fenced)
    rows = [scan(fenced, selected, limit=1) for _ in range(3)]
    assert [row["status"] for row in rows] == ["MORE", "MORE", "COMPLETED"]
    assert all(row["eligible_contracts"] == 3 for row in rows)
    assert len({signal for row in rows for signal in row["signals"]}) == 3
    assert scan(fenced, selected)["signals"] == []
    with fenced.admin() as conn:
        events = conn.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE source='quant'"
        ).fetchall()
        assert {row[0]["symbol"] for row in events} == {
            "BTCUSDT",
            "ETHUSDT",
            "牛来USDT",
        }
        assert all(row[0]["signal"] == "STRUCTURE_SCAN" for row in events)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_lost_checkpoint_replay_keeps_original_signals(fenced):
    selected = setup(fenced)
    first = scan(fenced, selected)
    with fenced.admin() as conn:
        conn.execute("DELETE FROM v2_strategy_scan_cursors")
    second = scan(fenced, selected)
    assert first["signals"] == second["signals"]


def test_stopped_scan_never_connects(fenced):
    selected = setup(fenced)
    stop = Event()
    stop.set()

    def forbidden():
        pytest.fail("stopped scanner accessed DB")

    result = scan_catalog(
        forbidden,
        binding=selected.producer.binding,
        instance_id=selected.producer.instance_id,
        guard=selected.producer.guard,
        stop=stop,
    )
    assert result["status"] == "STOPPED"


def test_partial_batch_rollback_does_not_skip_symbols(fenced):
    selected = setup(fenced)
    stop = Event()

    @contextmanager
    def interrupted():
        with fenced.admin() as conn:
            original = conn.execute

            class Connection:
                def execute(self, query, params=None):
                    result = original(query, params)
                    if (
                        isinstance(query, str)
                        and "INSERT INTO v2_inbound_signals" in query
                    ):
                        stop.set()
                    return result

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield Connection()

    with pytest.raises(ValueError, match="STRATEGY_SCAN_STOPPED"):
        scan_catalog(
            interrupted,
            binding=selected.producer.binding,
            instance_id=selected.producer.instance_id,
            guard=selected.producer.guard,
            stop=stop,
        )
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_inbound_signals WHERE source='quant'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_scan_cursors"
        ).fetchone() == (0,)
    assert len(scan(fenced, selected)["signals"]) == 3


def test_cross_window_slow_scan_visits_tail_with_current_window_identities(fenced):
    selected = setup(fenced, ("AAAUSDT", "BBBUSDT", "CCCUSDT"))
    window = (now(fenced.admin) // _WINDOW + 1) * _WINDOW
    observed = []
    for index in range(4):
        current = window + index * _WINDOW
        connect = advance_catalog(fenced, current + 10)
        result = scan(fenced, selected, connect=connect, limit=1)
        assert len(result["signals"]) == 1
        assert result["window_ms"] == current
        assert result["completion_scope"] == "CATALOG_ROTATION_NOT_WINDOW_COVERAGE"
        with fenced.admin() as conn:
            snapshot = conn.execute(
                "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s",
                (result["signals"][0],),
            ).fetchone()[0]
        assert (
            snapshot["observed_at"] == snapshot["features"]["closed_at_ms"] == current
        )
        assert snapshot["expires_at_ms"] == current + _WINDOW
        observed.append(snapshot["symbol"])
    assert observed == ["AAAUSDT", "BBBUSDT", "CCCUSDT", "AAAUSDT"]
    with fenced.admin() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_multiple_window_gap_finishes_original_rotation_then_idles(fenced):
    selected = setup(fenced, ("AAAUSDT", "BBBUSDT", "CCCUSDT"))
    first = scan(fenced, selected, limit=2)
    connect = advance_catalog(fenced, first["window_ms"] + 4 * _WINDOW + 10)
    second = scan(fenced, selected, connect=connect, limit=2)
    assert len(second["signals"]) == 1
    assert second["status"] == "COMPLETED"
    assert scan(fenced, selected, connect=connect)["signals"] == []
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT snapshot->>'symbol' FROM v2_inbound_signals WHERE signal_id=%s",
            (second["signals"][0],),
        ).fetchone() == ("CCCUSDT",)


def test_lost_commit_ack_across_window_continues_after_original_commit(fenced):
    selected = setup(fenced, ("AAAUSDT", "BBBUSDT", "CCCUSDT"))
    first = scan(fenced, selected, limit=1)
    connect = advance_catalog(fenced, first["window_ms"] + _WINDOW + 10)

    @contextmanager
    def lost():
        with connect() as conn:
            yield conn
        raise OSError("synthetic catalog commit ACK lost")

    with pytest.raises(OSError, match="ACK lost"):
        scan(fenced, selected, connect=lost, limit=1)
    # BBB was committed together with its cursor despite the missing response.
    third = scan(fenced, selected, connect=connect, limit=1)
    assert third["status"] == "COMPLETED"
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT snapshot->>'symbol' FROM v2_inbound_signals WHERE source='quant' ORDER BY snapshot->>'symbol'"
        ).fetchall() == [("AAAUSDT",), ("BBBUSDT",), ("CCCUSDT",)]


def test_catalog_shrink_completes_old_seek_then_next_window_restarts(fenced):
    selected = setup(fenced, ("AAAUSDT", "BBBUSDT", "CCCUSDT"))
    first = scan(fenced, selected, limit=2)
    tick = first["window_ms"] + _WINDOW + 10
    catalog = Catalog(fenced.admin, "SANDBOX", lambda: tick)
    row = catalog.read(2 * _WINDOW)["AAAUSDT"]
    catalog.save({"symbols": [row]})
    connect = at_database_time(fenced.admin, tick)
    assert scan(fenced, selected, connect=connect)["signals"] == []
    next_connect = advance_catalog(fenced, tick + _WINDOW)
    assert len(scan(fenced, selected, connect=next_connect)["signals"]) == 1


@pytest.mark.parametrize("end_offset", [-1, _WINDOW])
def test_window_crossing_or_reversal_rolls_back_observations_and_cursor(
    fenced, end_offset
):
    selected = setup(fenced)
    window = (now(fenced.admin) // _WINDOW + 1) * _WINDOW
    calls = []

    @contextmanager
    def changing_clock():
        with fenced.admin() as conn:

            class Connection:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, query, *args, **kwargs):
                    if query == _CLOCK:
                        tick = window + (10 if not calls else end_offset)
                        calls.append(tick)
                        return conn.execute("SELECT %s::bigint", (tick,))
                    return conn.execute(query, *args, **kwargs)

            yield Connection()

    advance_catalog(fenced, window + 10)
    with pytest.raises(ValueError, match="WINDOW_EXPIRED"):
        scan(fenced, selected, connect=changing_clock, limit=2)
    with fenced.admin() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_scan_cursors"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_inbound_signals WHERE source='quant'"
        ).fetchone() == (0,)
