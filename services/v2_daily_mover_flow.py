"""Optional, checkpointed public flow research; never part of order execution."""

import time
from collections import Counter
from copy import deepcopy
from datetime import date

from services.v2_daily_mover_market import MoverMarketError
from services.v2_daily_mover_precursors import CUTOFF_HOURS, HOUR
from v2_core.daily_movers import DAY, _valid_symbol, previous_day_window
from v2_core.evidence import canonical, digest
from v2_core.mover_flow import analyze_flow_evidence

VERSION = "daily-mover-flow-review-v1"
_HALTS = {
    "MOVER_RATE_LIMITED",
    "MOVER_SHARED_QUOTA_WAIT",
    "MOVER_JOB_DEADLINE",
    "MOVER_PUBLIC_TRANSPORT_FAILURE",
    "MOVER_FLOW_REQUEST_LIMIT",
    "MOVER_SHARED_QUOTA_FAILURE",
    "MOVER_FLOW_CLOCK_INVALID",
}


def unavailable_review(reason):
    report = {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": reason,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
    }
    return {**report, "content_digest": digest(canonical(report))}


class DailyMoverFlowReview:
    def __init__(
        self,
        store,
        market_factory,
        *,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        max_symbols=40,
        retry_seconds=3600,
    ):
        if not callable(market_factory) or not callable(clock_ms):
            raise TypeError("FLOW_PORT_REQUIRED")
        if (
            type(max_symbols) is not int
            or not 1 <= max_symbols <= 40
            or type(retry_seconds) is not int
            or not 3600 <= retry_seconds <= 86400
        ):
            raise ValueError("FLOW_REVIEW_BOUNDS")
        self.store, self.market_factory, self.clock = store, market_factory, clock_ms
        self.max_symbols, self.retry_ms = max_symbols, retry_seconds * 1000
        self.market = None
        self.halted = None

    def _cases(self, payload, start):
        result = []
        for hour in CUTOFF_HOURS:
            for side in ("BUY", "SELL"):
                result.append(
                    analyze_flow_evidence(
                        payload, cutoff_ms=start + hour * HOUR, side=side
                    )
                )
        return result

    def _complete(self, payload, cases):
        absent_spot = payload.get("mapping", {}).get("reason") in {
            "NO_EXACT_SPOT_MAPPING",
            "EXACT_SPOT_ASSET_MISMATCH",
        }
        return all(
            case["components"][name].get("status") == "OBSERVED"
            or (name == "spot" and absent_spot)
            for case in cases
            for name in ("spot", "open_interest", "funding")
        )

    def _preserve_observations(self, record, payload, assessed, start):
        """Keep better prior components with their original receipt timestamps."""
        if record is None:
            return payload, assessed
        previous = record["payload"]
        if any(
            previous.get(k) != payload.get(k)
            for k in ("version", "source", "symbol", "start_ms", "end_ms")
        ):
            raise ValueError("FLOW_RETRY_SCOPE_MISMATCH")
        before = self._cases(previous, start)
        retained = []
        combined = deepcopy(payload)
        for name in ("spot", "open_interest", "funding"):
            loses_known_window = any(
                old["components"][name]["status"] == "OBSERVED"
                and new["components"][name]["status"] != "OBSERVED"
                for old, new in zip(before, assessed, strict=True)
            )
            if loses_known_window:
                combined[name] = deepcopy(previous[name])
                if name == "spot":
                    combined["mapping"] = deepcopy(previous["mapping"])
                retained.append(name)
        if retained:
            combined["retry_evidence"] = {
                "retained_components": retained,
                "previous_payload_digest": record["input_digest"],
                "attempt": {
                    name: {
                        key: payload[name].get(key)
                        for key in ("status", "reason", "fetched_at_ms")
                    }
                    for name in ("spot", "open_interest", "funding")
                },
            }
            assessed = self._cases(combined, start)
        return combined, assessed

    def build(self, *, window, precursor):
        start, end, day = (window.get(key) for key in ("start_ms", "end_ms", "date"))
        now = self.clock()
        if (
            type(start) is not int
            or type(end) is not int
            or type(now) is not int
            or not 0 <= start < end <= now < 2**53
            or end - start != DAY
            or (start + 8 * HOUR) % DAY
            or not isinstance(day, str)
            or date.fromisoformat(day).isoformat() != day
        ):
            raise ValueError("FLOW_REVIEW_DAY_INVALID")
        if previous_day_window(end)["date"] != day:
            raise ValueError("FLOW_REVIEW_DAY_INVALID")
        if (
            not isinstance(precursor, dict)
            or precursor.get("status") != "DESCRIPTIVE_RESEARCH_ONLY"
        ):
            return unavailable_review("PRECURSOR_SELECTION_UNAVAILABLE")
        cohorts, tops = (
            precursor.get("fixed_cohort"),
            precursor.get("top_mover_diagnostics"),
        )
        if (
            not isinstance(cohorts, list)
            or not isinstance(tops, list)
            or len(cohorts) > 40
            or len(tops) > 20
            or any(not _valid_symbol(s) for s in cohorts + tops)
        ):
            raise ValueError("FLOW_SELECTION_INVALID")
        selected = sorted(set(cohorts + tops))
        if len(selected) > self.max_symbols:
            # Never truncate alphabetically or silently drop failed controls.
            return unavailable_review("FLOW_SELECTION_EXCEEDS_BUDGET")
        cases, provenance, unknown, halted = [], {}, {}, self.halted
        storage_failed = False
        for symbol in selected:
            record = None
            if not storage_failed:
                try:
                    record = self.store.read(day, symbol)
                except Exception:  # noqa: BLE001 - optional storage cannot block the primary report
                    storage_failed, halted = True, "FLOW_STORAGE_UNAVAILABLE"
            if record is None and storage_failed:
                unknown[symbol] = "FLOW_STORAGE_UNAVAILABLE"
                continue
            due = record is None or (
                not record["complete"]
                and record["attempts"] < 3
                and now - record["updated_at_ms"] >= self.retry_ms
            )
            if due and halted is None:
                try:
                    if self.market is None:
                        self.market = self.market_factory()
                    payload = self.market.collect(symbol, start_ms=start, end_ms=end)
                    deferred = next(
                        (
                            envelope.get("reason")
                            for envelope in [
                                payload.get("mapping", {}),
                                *(
                                    payload.get(n, {})
                                    for n in ("spot", "open_interest", "funding")
                                ),
                            ]
                            if envelope.get("reason") in _HALTS
                        ),
                        None,
                    )
                    assessed = self._cases(payload, start)
                    payload, assessed = self._preserve_observations(
                        record, payload, assessed, start
                    )
                    record = self.store.save(
                        day, symbol, payload, complete=self._complete(payload, assessed)
                    )
                    if deferred:
                        halted = self.halted = deferred
                except MoverMarketError as exc:
                    code = str(exc)
                    halted = self.halted = (
                        code if code in _HALTS else "FLOW_PUBLIC_READ_UNAVAILABLE"
                    )
                except Exception:  # noqa: BLE001 - no raw HTTP/DB errors in research evidence
                    halted = self.halted = "FLOW_RESEARCH_COMPONENT_UNAVAILABLE"
            if record is None:
                unknown[symbol] = halted or "FLOW_CHECKPOINT_UNAVAILABLE"
                continue
            payload = record["payload"]
            if (
                payload.get("symbol") != symbol
                or payload.get("start_ms") != start
                or payload.get("end_ms") != end
            ):
                unknown[symbol] = "FLOW_CHECKPOINT_SCOPE_MISMATCH"
                continue
            try:
                assessed = self._cases(payload, start)
            except Exception:  # noqa: BLE001 - damaged data is UNKNOWN, not a trading stop
                unknown[symbol] = "FLOW_CHECKPOINT_ANALYSIS_UNAVAILABLE"
                continue
            provenance[symbol] = record["input_digest"]
            for case in assessed:
                cases.append(
                    {
                        "symbol": symbol,
                        "side": case["side"],
                        "cutoff_ms": case["cutoff_ms"],
                        "fixed_cohort": symbol in cohorts,
                        "top_mover_diagnostic": symbol in tops,
                        "status": case["status"],
                        "components": case["components"],
                        "input_digest": case["input_digest"],
                    }
                )
        for symbol, reason in unknown.items():
            for hour in CUTOFF_HOURS:
                for side in ("BUY", "SELL"):
                    cases.append(
                        {
                            "symbol": symbol,
                            "side": side,
                            "cutoff_ms": start + hour * HOUR,
                            "fixed_cohort": symbol in cohorts,
                            "top_mover_diagnostic": symbol in tops,
                            "status": "UNKNOWN",
                            "input_digest": None,
                            "components": {
                                name: {
                                    "status": "UNKNOWN",
                                    "reason": reason,
                                    "features": None,
                                    "receipt": None,
                                    "input_digest": None,
                                }
                                for name in ("spot", "open_interest", "funding")
                            },
                        }
                    )
        cases.sort(key=lambda case: (case["symbol"], case["cutoff_ms"], case["side"]))
        counts = {
            name: dict(Counter(c["components"][name]["status"] for c in cases))
            for name in ("spot", "open_interest", "funding")
        }
        retention = "UNKNOWN"
        if not storage_failed:
            try:
                self.store.prune_raw(day, limit=500)
                retention = "COMPLETED"
            except Exception:  # noqa: BLE001 - bounded optional retention is isolated
                retention = "UNKNOWN"
        report = {
            "version": VERSION,
            "status": "DESCRIPTIVE_RESEARCH_ONLY",
            "source": "BINANCE_LIVE_PUBLIC",
            "selected_symbols": selected,
            "source_digests": provenance,
            "unknown_symbols": unknown,
            "component_status_counts": counts,
            "checkpoint_retention": retention,
            "cases": cases,
            "execution_authorized": False,
            "automatic_parameter_updates": False,
            "limitations": [
                "Backfilled event-time observations do not prove live receipt at the cutoff.",
                "Spot aggressor imbalance is not capital inflow, participant inventory or intent.",
                "OI and actual funding alone do not establish liquidations or squeeze mechanics.",
                "Exact current spot mapping does not prove historical instrument availability.",
                "No continuous book reconstruction; replenishment and absorption remain unknown.",
                "No model fitted or calibrated; compare held-out failures and controls before entry influence.",
                "LIVE public evidence is not SANDBOX execution PnL; reference cohort is not liquidity-matched.",
            ],
        }
        return {**report, "content_digest": digest(canonical(report))}
