import pytest
from test_v2_quant_indicators import minutes
from test_v2_shared_market import CH

from services.v2_quant_archive_replay import run
from v2_core.evidence import canonical, digest


def test_archive_replay_requires_complete_verified_input_and_never_writes():
    client = CH()
    for bar in minutes(breakout=True):
        payload = canonical(bar)
        client.rows.append(("SANDBOX", "BTCUSDT", bar["t"], payload, digest(payload)))
    report = run(
        client,
        symbols=("BTCUSDT",),
        timeframe=15,
        end_ms=86400000,
        minutes=1440,
        plugins=("donchian",),
    )
    assert report["execution_authorized"] is False
    assert report["reports"][0]["status"] == "SIMULATED"
    assert report["reports"][0]["bar_count"] == 96
    assert report["input_closed_at_utc8"].endswith("+08:00")
    assert client.inserts == []
    client.rows.pop(0)
    assert (
        run(client, symbols=("BTCUSDT",), timeframe=15, end_ms=86400000, minutes=1440)[
            "reports"
        ][0]["status"]
        == "INCOMPLETE_HISTORY"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"symbols": ()},
        {"symbols": ("BTCUSDT",) * 11},
        {"timeframe": 0},
        {"minutes": 20000},
        {"plugins": ("untrusted-code",)},
    ],
)
def test_archive_replay_has_bounded_scope(changes):
    with pytest.raises(ValueError):
        run(
            CH(),
            **{"symbols": ("BTCUSDT",), "timeframe": 5, "end_ms": 86400000, **changes},
        )
