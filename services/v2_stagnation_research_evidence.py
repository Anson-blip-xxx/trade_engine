"""Bounded frozen exit evidence for independent research, never order authority.

An archive downloaded later cannot prove what was available at an exit. Keep
missing receipt/protection evidence unknown rather than reconstructing it from
today's market or the fact that a trade eventually closed.
"""

from dataclasses import asdict

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.state import StateKey

VERSION = "stagnation-frozen-evidence-v2"
_SQL = """SELECT account_scope,episode,facts,decision,policy,observation,
    native_protection,hourly_history,
    (extract(epoch FROM recorded_at)*1000)::bigint,
    to_jsonb(frozen)->'research_snapshot',
    to_jsonb(frozen)->>'research_snapshot_digest'
FROM v2_stagnation_frozen_exit_evidence frozen
WHERE state_id=%s AND state_scope=%s::jsonb
  AND recorded_at>=to_timestamp(%s/1000.0)
  AND recorded_at<=to_timestamp(%s/1000.0)
  AND decision->>'reason'='LOW_YIELD_STAGNATION'
ORDER BY version LIMIT 1"""


def _result(status, reason, **extra):
    return {
        "version": VERSION,
        "status": status,
        "reason": reason,
        "execution_authorized": False,
        "promotion_authorized": False,
        **extra,
    }


def _stamp(value):
    return type(value) is int and 0 <= value < 2**53


def load_frozen_exit(conn, scope, trade, *, as_of_ms):
    """One indexed query and savepoint, inside the caller's read-only snapshot."""
    if trade.get("exit_reason") != "LOW_YIELD_STAGNATION":
        return _result("NOT_APPLICABLE", "BASELINE_NOT_STAGNATION")
    if (
        not isinstance(scope, AccountScope)
        or scope.environment != "SANDBOX"
        or not _stamp(as_of_ms)
        or not _stamp(trade.get("opened_ms"))
        or not _stamp(trade.get("closed_ms"))
        or not trade["opened_ms"] <= trade["closed_ms"] <= as_of_ms
        or not isinstance(trade.get("id"), str)
        or not 1 <= len(trade["id"]) <= 128
    ):
        return _result("UNKNOWN", "FROZEN_EXIT_SCOPE_OR_WINDOW_INVALID")
    key = StateKey(**asdict(scope), namespace="directional-exit-v1", key=trade["id"])
    try:
        with conn.transaction():
            saved = conn.execute(
                _SQL,
                (key.identity, canonical(asdict(key)), trade["opened_ms"], as_of_ms),
            ).fetchone()
    except Exception:  # noqa: BLE001 - research failure cannot erase trade evidence
        return _result("UNKNOWN", "FROZEN_EXIT_QUERY_UNAVAILABLE")
    if saved is None:
        return _result("UNKNOWN", "FROZEN_EXIT_NOT_RETAINED")
    try:
        (
            account,
            episode,
            facts,
            baseline,
            policy,
            observed,
            protection,
            hours,
            recorded,
        ) = saved[:9]
        if (
            account != asdict(scope)
            or episode != trade["id"]
            or not all(isinstance(v, dict) for v in (facts, baseline, policy, observed))
            or observed.get("symbol") != trade.get("symbol")
            or observed.get("environment") != scope.environment
            or facts.get("side") != trade.get("side")
            or not _stamp(facts.get("held_ms"))
            or not _stamp(recorded)
            or not trade["opened_ms"] <= recorded <= as_of_ms
        ):
            raise ValueError("invalid frozen scope")
        at = trade["opened_ms"] + facts["held_ms"]
        if not trade["opened_ms"] <= at <= min(trade["closed_ms"], recorded):
            raise ValueError("invalid frozen decision time")
        # Environment is explicit in the frozen exit input. The original
        # indicator version is retained separately, never silently upgraded.
        observation = {
            field: observed.get(field)
            for field in (
                "observed_at_ms",
                "available_at_ms",
                "closed_15m_at_ms",
                "closed_1h_at_ms",
                "environment",
            )
        }
        observation["market_source"] = "BINANCE_USDM_SANDBOX"
        inputs = {
            "opened_at_ms": trade["opened_ms"],
            "snapshots": [
                {
                    "at_ms": at,
                    "facts": facts,
                    "baseline": baseline,
                    "baseline_policy": policy,
                    "observation": observation,
                    "native_protection": protection,
                    "hourly_history": hours if isinstance(hours, list) else [],
                }
            ],
        }
        capture = saved[9] if len(saved) > 9 else None
        capture_summary = None
        if capture is not None:
            snapshot, capture_summary = _prospective_snapshot(
                capture,
                saved[10] if len(saved) > 10 else None,
                scope=scope,
                trade=trade,
                at=at,
                facts=facts,
                baseline=baseline,
                policy=policy,
                observation=observed,
            )
            if snapshot is not None:
                inputs["snapshots"] = [snapshot]
        if len(canonical(inputs).encode()) > 131072:
            raise ValueError("frozen input bound")
    except (TypeError, ValueError, KeyError, ArithmeticError):
        return _result("UNKNOWN", "FROZEN_EXIT_EVIDENCE_INVALID")
    return _result(
        "AVAILABLE",
        None,
        payload=inputs,
        input_digest=digest(canonical(inputs)),
        recorded_at_ms=recorded,
        indicator_version=observed.get("source"),
        historical_receipt_proven=False,
        prospective_capture=capture_summary,
    )


def _prospective_snapshot(
    capture, fingerprint, *, scope, trade, at, facts, baseline, policy, observation
):
    """A new projection never upgrades old evidence or changes its decision time."""
    if (
        not isinstance(capture, dict)
        or capture.get("version") != "prospective-exit-research-snapshot-v1"
        or capture.get("execution_authorized") is not False
        or capture.get("promotion_authorized") is not False
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or len(canonical(capture).encode()) > 65536
        or digest(canonical(capture)) != fingerprint
    ):
        raise ValueError("prospective capture digest")
    if (
        capture.get("status") == "UNKNOWN"
        and capture.get("snapshot") is None
        and capture.get("binding") is None
    ):
        # Expected fail-soft producer result: do not erase an independently
        # valid parent baseline, and do not merge any optional proof into it.
        return None, {
            "status": "UNKNOWN",
            "reason": "CAPTURE_BASELINE_UNAVAILABLE",
            "digest_verified": True,
            "same_frozen_decision": False,
            "component_statuses": {},
        }
    binding = capture.get("binding")
    expected = {
        "episode": trade["id"],
        "symbol": trade["symbol"],
        "environment": scope.environment,
        "account_scope_digest": digest(canonical(asdict(scope))),
        "opened_at_ms": trade["opened_ms"],
        "at_ms": at,
    }
    if binding != expected:
        raise ValueError("prospective capture binding")
    raw = capture.get("snapshot")
    if not isinstance(raw, dict) or set(raw) != {
        "at_ms",
        "facts",
        "baseline",
        "baseline_policy",
        "observation",
        "native_protection",
        "hourly_history",
    }:
        raise ValueError("prospective capture fields")
    if (
        raw["at_ms"] != at
        or raw["facts"] != facts
        or raw["baseline"] != baseline
        or raw["baseline_policy"] != policy
    ):
        raise ValueError("prospective capture baseline")
    observed = raw["observation"]
    if isinstance(observed, dict):
        for name in (
            "observed_at_ms",
            "closed_15m_at_ms",
            "closed_1h_at_ms",
            "environment",
        ):
            if observed.get(name) != observation.get(name):
                raise ValueError("prospective observation identity")
        available = observed.get("available_at_ms")
        if available is not None and (not _stamp(available) or available > at):
            raise ValueError("prospective observation availability")
    components = capture.get("components")
    summary = {
        "digest_verified": True,
        "same_frozen_decision": True,
        "component_statuses": {
            name: item.get("status")
            for name, item in (
                components.items() if isinstance(components, dict) else ()
            )
            if name
            in {"observation", "hourly_history", "native_terms", "native_quantity"}
            and isinstance(item, dict)
            and item.get("status") in {"AVAILABLE", "UNKNOWN", "PARTIAL", "UNAVAILABLE"}
        },
    }
    return raw, summary


def compare_frozen_exit(evidence):
    """Call the shadow engine without promoting missing historical evidence."""
    if not isinstance(evidence, dict):
        return _result("UNKNOWN", "FROZEN_EXIT_EVIDENCE_NOT_LOADED")
    if evidence.get("status") != "AVAILABLE":
        return {key: value for key, value in evidence.items() if key != "payload"}
    from services.v2_trend_runner_shadow import compare_stagnation_candidates
    from v2_core.trend_runner import StagnationShadowPolicy

    try:
        payload = evidence["payload"]
        if evidence.get("input_digest") != digest(canonical(payload)):
            raise ValueError("frozen input changed")
        compared = compare_stagnation_candidates(
            [payload], policy=StagnationShadowPolicy(mode="SHADOW"), max_candidates=1
        )
        return {
            **{key: value for key, value in evidence.items() if key != "payload"},
            "comparison": compared,
            "actual_pnl": False,
            "counterfactual_pnl_proven": False,
            "historical_code_identity_proven": False,
        }
    except Exception:  # noqa: BLE001 - this optional arm never breaks other research
        return _result("UNKNOWN", "STAGNATION_COMPARISON_UNAVAILABLE")
