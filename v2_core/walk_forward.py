"""Bounded single-series walk-forward research, with no execution or I/O.

Training expands from the first bar. Full OOS windows do not overlap; each
starts flat with its own copy of the configured initial equity. The gap is
excluded from selection, but its closed bars may seed OOS features. The last
pre-test close can signal an entry at the first test open, never before it.

A future fitted entry gate must fit on training only, freeze before OOS, and
be applied to every comparison consistently. No HMM or fitted gate exists here.
"""

import re
from dataclasses import asdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal
from v2_core.quant_registry import PLUGINS, REGISTRY_VERSION, parameters, warmup
from v2_core.quant_replay import ReplayPolicy, replay

VERSION = "quant-walk-forward-v1"
MAX_CANDIDATES = 8
MAX_FOLDS = 20
MAX_BARS = 10000
MAX_REPLAY_CALLS = 160
MAX_REPLAY_BARS = 200000
MAX_FEATURE_BAR_VISITS = 2000000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_TIMEFRAMES = (1, 5, 15, 30, 60, 240, 1440)


def _text(value):
    value = Fraction(value)
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _candidates(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_CANDIDATES:
        raise ValueError("WALK_FORWARD_CANDIDATES_BOUND")
    normalized = []
    seen = set()
    for value in values:
        if (
            not isinstance(value, dict)
            or set(value) - {"id", "plugin", "params"}
            or not {"id", "plugin"} <= value.keys()
            or not isinstance(value["id"], str)
            or not _ID.fullmatch(value["id"])
            or value["id"] in seen
            or value["plugin"] not in PLUGINS
        ):
            raise ValueError("WALK_FORWARD_CANDIDATE")
        config = {
            "id": value["id"],
            "plugin": value["plugin"],
            "params": parameters(value.get("params")),
        }
        seen.add(config["id"])
        normalized.append(
            {
                **config,
                "parameters_digest": digest(canonical(config["params"])),
                "candidate_digest": digest(
                    canonical({"registry_version": REGISTRY_VERSION, **config})
                ),
                "warmup_bars": warmup(config["params"]),
            }
        )
    return sorted(normalized, key=lambda candidate: candidate["id"])


def _funding(events, bars, width):
    if not isinstance(events, (list, tuple)) or len(events) > MAX_BARS:
        raise ValueError("WALK_FORWARD_FUNDING_BOUND")
    normalized = []
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event) != {"at_ms", "rate", "mark_price"}
            or type(event["at_ms"]) is not int
            or not bars
            or not bars[0].t <= event["at_ms"] < bars[-1].t + width
            or type(event["rate"]) is not str
            or type(event["mark_price"]) is not str
        ):
            raise ValueError("QUANT_FUNDING_INVALID")
        if event["at_ms"] % width:
            raise ValueError("QUANT_FUNDING_REQUIRES_FINER_BARS")
        if abs(decimal(event["rate"])) > 1 or decimal(event["mark_price"]) <= 0:
            raise ValueError("QUANT_FUNDING_INVALID")
        normalized.append(dict(event))
    normalized.sort(key=lambda event: event["at_ms"])
    if len({event["at_ms"] for event in normalized}) != len(normalized):
        raise ValueError("QUANT_FUNDING_DUPLICATE")
    return normalized


def _window(bars, start, end, width):
    return {
        "start_index": start,
        "end_index_exclusive": end,
        "bar_count": end - start,
        "start_ms": bars[start].t,
        "end_ms_exclusive": bars[end - 1].t + width,
    }


def _input_digest(bars, funding):
    return digest(
        canonical({"bars": [bar.payload() for bar in bars], "funding": funding})
    )


def _run(bars, *, candidate, timeframe, policy, funding, start_ms, end_ms):
    events = [event for event in funding if bars[0].t <= event["at_ms"] < end_ms]
    result = replay(
        bars,
        timeframe_minutes=timeframe,
        plugin=candidate["plugin"],
        params=candidate["params"],
        policy=policy,
        funding=events,
        entry_start_ms=start_ms,
        entry_end_ms=end_ms,
    )
    # Flat warmup must not look like an OOS equity history or an earlier trade.
    result["curve"] = [
        point for point in result["curve"] if start_ms < point["closed_at_ms"] <= end_ms
    ]
    result["decisions"] = [
        decision
        for decision in result["decisions"]
        if start_ms <= decision["closed_at_ms"] <= end_ms
    ]
    period_events = [event for event in events if event["at_ms"] >= start_ms]
    flags = ["FUNDING_COMPLETENESS_UNVERIFIED" if period_events else "FUNDING_MISSING"]
    if not result["entries"]:
        flags.append("NO_ENTRIES")
    if not result["trades"]:
        flags.append("NO_CLOSED_TRADES")
    if result["open_position"] is not None:
        flags.append("OPEN_POSITION_AT_BOUNDARY")
    if not result["decisions"]:
        flags.append("INSUFFICIENT_FEATURE_WARMUP")
    net = Fraction(result["summary"]["equity"]) - Fraction(policy.initial_equity)
    return {
        "candidate_id": candidate["id"],
        "candidate_digest": candidate["candidate_digest"],
        "input_digest": _input_digest(bars, events),
        "input_start_ms": bars[0].t,
        "entry_start_ms": start_ms,
        "entry_end_ms_exclusive": end_ms,
        "initial_equity": policy.initial_equity,
        "starts_flat": True,
        "net_marked_pnl": _text(net),
        "net_marked_return_fraction": _text(net / Fraction(policy.initial_equity)),
        "funding_event_count": len(period_events),
        "funding_complete": False,
        "flags": flags,
        "replay": result,
        "execution_authorized": False,
    }


def walk_forward(
    bars,
    *,
    timeframe_minutes,
    candidates,
    train_bars,
    test_bars,
    gap_bars=0,
    policy=None,
    funding=(),
):
    """Select by training net marked PnL; break equal scores by candidate id.

    Only complete test windows are simulated. Results are individual research
    experiments, not a stitched portfolio. Funding completeness cannot be
    established from the supplied events alone, including an empty event list.
    """
    if (
        not isinstance(bars, (list, tuple))
        or len(bars) > MAX_BARS
        or type(timeframe_minutes) is not int
        or timeframe_minutes not in _TIMEFRAMES
        or any(
            type(value) is not int or not low <= value <= MAX_BARS
            for value, low in ((train_bars, 1), (test_bars, 1), (gap_bars, 0))
        )
    ):
        raise ValueError("WALK_FORWARD_INPUT_BOUND")
    policy = ReplayPolicy() if policy is None else policy
    if not isinstance(policy, ReplayPolicy):
        raise TypeError("WALK_FORWARD_POLICY")
    choices = _candidates(candidates)
    width = timeframe_minutes * 60000
    # Match replay's strict contiguous-bar contract, also for an unused tail.
    if any(not isinstance(bar, Bar) or bar.t % width for bar in bars) or any(
        bars[i].t != bars[i - 1].t + width for i in range(1, len(bars))
    ):
        raise ValueError("QUANT_REPLAY_GAP")
    events = _funding(funding, bars, width)
    fold_count = max(0, (len(bars) - train_bars - gap_bars) // test_bars)
    if fold_count > MAX_FOLDS:
        raise ValueError("WALK_FORWARD_FOLDS_BOUND")
    calls = fold_count * len(choices) * 2
    replay_bars, feature_visits = 0, 0
    for index in range(fold_count):
        train_end = train_bars + index * test_bars
        test_start = train_end + gap_bars
        for candidate in choices:
            seed = min(test_start, candidate["warmup_bars"])
            for count in (train_end, seed + test_bars):
                replay_bars += count
                feature_visits += (
                    max(0, count - candidate["warmup_bars"] + 1)
                    * candidate["warmup_bars"]
                )
    if (
        calls > MAX_REPLAY_CALLS
        or replay_bars > MAX_REPLAY_BARS
        or feature_visits > MAX_FEATURE_BAR_VISITS
    ):
        raise ValueError("WALK_FORWARD_WORK_BOUND")
    policy_values = asdict(policy)
    policy_digest = digest(canonical(policy_values))
    assessed_end = train_bars + gap_bars + fold_count * test_bars
    trailing = max(0, len(bars) - assessed_end)
    flags = ["FUNDING_COMPLETENESS_UNVERIFIED" if events else "FUNDING_MISSING"]
    if not fold_count:
        flags.append("NO_COMPLETE_OOS_FOLD")
    if trailing:
        flags.append("PARTIAL_OOS_NOT_EVALUATED")
    report = {
        "version": VERSION,
        "registry_version": REGISTRY_VERSION,
        "status": "SIMULATED" if fold_count else "INSUFFICIENT_DATA",
        "scope": "SINGLE_SERIES_INDEPENDENT_FOLDS_NOT_PORTFOLIO",
        "timeframe_minutes": timeframe_minutes,
        "window_policy": {
            "training": "EXPANDING_FROM_FIRST_BAR",
            "initial_train_bars": train_bars,
            "test_bars": test_bars,
            "gap_bars": gap_bars,
            "test_intervals": "START_INCLUSIVE_END_EXCLUSIVE_NONOVERLAPPING",
            "incomplete_test_windows": "NOT_SIMULATED",
            "warmup": "PRE_TEST_HISTORY_FEATURES_ONLY_INCLUDING_GAP",
            "training_positions": "NOT_CARRIED_TO_TEST_OR_FORCED_CLOSED",
        },
        "selection_policy": {
            "metric": "TRAIN_NET_MARKED_PNL_AFTER_CONFIGURED_COSTS",
            "tie_break": "CANDIDATE_ID_ASCENDING",
            "uses_test_data": False,
            "includes_unrealized_pnl": True,
        },
        "policy": policy_values,
        "policy_digest": policy_digest,
        "candidates": choices,
        "input_digest": _input_digest(bars, events),
        "bar_count": len(bars),
        "fold_count": fold_count,
        "unevaluated_tail_bars": trailing,
        "required_bars_for_first_fold": train_bars + gap_bars + test_bars,
        "work": {
            "candidate_replay_calls": calls,
            "replayed_bars": replay_bars,
            "feature_bar_visits": feature_visits,
        },
        "funding_complete": False,
        "flags": flags,
        "folds": [],
        "portfolio_equity": None,
        "entry_gate": {
            "status": "NOT_APPLIED",
            "hmm_fitted": False,
            "future_contract": "FIT_ON_TRAIN_ONLY_FREEZE_BEFORE_TEST",
        },
        "limitations": [
            "Simulated outcomes and configured fees/slippage are not live execution evidence.",
            "Funding completeness and historical contract eligibility are not verified.",
            "Open positions remain marked and are not presented as realized trades.",
            "Independent folds and symbols must not be summed as portfolio equity.",
            "Trying candidate variants is model selection; OOS baselines must not select this fold's winner.",
            "No statistical significance, calibrated probability or HMM fit is asserted.",
        ],
        "execution_authorized": False,
    }
    for index in range(fold_count):
        train_end = train_bars + index * test_bars
        test_start = train_end + gap_bars
        test_end = test_start + test_bars
        train_window = _window(bars, 0, train_end, width)
        test_window = _window(bars, test_start, test_end, width)
        trials = []
        for candidate in choices:
            trial = _run(
                bars[:train_end],
                candidate=candidate,
                timeframe=timeframe_minutes,
                policy=policy,
                funding=events,
                start_ms=train_window["start_ms"],
                end_ms=train_window["end_ms_exclusive"],
            )
            trial["eligible_for_selection"] = train_end > candidate["warmup_bars"]
            if not trial["eligible_for_selection"]:
                trial["flags"].append("INSUFFICIENT_TRAIN_WARMUP")
            trials.append(trial)
        eligible = [trial for trial in trials if trial["eligible_for_selection"]]
        winner = (
            min(
                eligible,
                key=lambda trial: (
                    -Fraction(trial["net_marked_pnl"]),
                    trial["candidate_id"],
                ),
            )
            if eligible
            else None
        )
        # Freeze selection before computing any candidate's out-of-sample result.
        selection = None
        if winner:
            candidate = next(
                item for item in choices if item["id"] == winner["candidate_id"]
            )
            selection = {
                **candidate,
                "policy_digest": policy_digest,
                "training_input_digest": winner["input_digest"],
                "training_score": winner["net_marked_pnl"],
                "selected_at_ms": train_window["end_ms_exclusive"],
            }
            selection["selection_digest"] = digest(canonical(selection))
        baselines = []
        for candidate in choices:
            seed_start = max(0, test_start - candidate["warmup_bars"])
            baseline = _run(
                bars[seed_start:test_end],
                candidate=candidate,
                timeframe=timeframe_minutes,
                policy=policy,
                funding=events,
                start_ms=test_window["start_ms"],
                end_ms=test_window["end_ms_exclusive"],
            )
            baseline["warmup_bars"] = test_start - seed_start
            baseline["warmup_start_ms"] = bars[seed_start].t
            baselines.append(baseline)
        selected_oos = next(
            (
                baseline
                for baseline in baselines
                if selection and baseline["candidate_id"] == selection["id"]
            ),
            None,
        )
        fold_events = [
            event
            for event in events
            if event["at_ms"] < test_window["end_ms_exclusive"]
        ]
        report["folds"].append(
            {
                "index": index,
                "status": "SIMULATED" if selection else "INSUFFICIENT_TRAIN_DATA",
                "train": train_window,
                "test": test_window,
                "gap_bars": gap_bars,
                "policy": policy_values,
                "policy_digest": policy_digest,
                "input_digest": _input_digest(bars[:test_end], fold_events),
                "candidate_trials": trials,
                "selection": selection,
                "out_of_sample": selected_oos,
                "candidate_oos_baselines": baselines,
                "cash_baseline": {
                    "initial_equity": policy.initial_equity,
                    "final_equity": policy.initial_equity,
                    "net_pnl": "0",
                    "return_fraction": "0",
                    "interest": "NOT_MODELED",
                    "starts_flat": True,
                },
                "execution_authorized": False,
            }
        )
    return report
