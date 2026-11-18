"""Narrow live structural-veto evidence, never an execution opportunity claim.

The shadow observer may have recorded an unfilled OPEN decision. Its claims are
not execution claims and are deliberately not read here. Existing account locks
and one-position slots still arbitrate admission; the last actually filled and
settled episode is the next structural anchor. A READY proof is not a permit.
"""

from v2_core.evidence import canonical, digest

SOURCE = "entry-structure-online-v1"


def seal(proof):
    body = {key: value for key, value in proof.items() if key != "digest"}
    return {**body, "digest": digest(canonical(body))}


def validate(
    proof,
    *,
    scope,
    target,
    side,
    observed,
    now,
    expected_control_digest=None,
    expected_controls=None,
):
    """Return only the explicitly enabled veto; None never authorizes an order.

    Legacy CHASE_ONLY keeps its narrow rollout. TRADE_CYCLE declines WAIT and
    UNKNOWN candidates locally; only a verified first entry is not applicable.
    Neither mode grants execution authority or bypasses other safety checks.
    """
    fields = (
        "observed_at_ms",
        "finished_at_ms",
        "as_of_ms",
        "valid_until_ms",
        "market_cutoff_ms",
        "market_observed_at_ms",
    )
    if (
        not isinstance(proof, dict)
        or proof.get("source") != SOURCE
        or proof.get("account_scope") != scope
        or scope.get("environment") != "SANDBOX"
        or proof.get("symbol") != target
        or side not in {"BUY", "SELL"}
        or proof.get("side") != side
        or proof.get("strategy") != ("S6" if side == "BUY" else "S8")
        or proof.get("signal_observed_ms") != observed
        or proof.get("execution_authorized") is not False
        or proof.get("mode") not in {"SHADOW", "ENFORCE"}
        or any(type(proof.get(key)) is not int for key in fields)
        or not 0
        <= proof["market_cutoff_ms"]
        <= proof["market_observed_at_ms"]
        <= proof["observed_at_ms"]
        or proof["market_cutoff_ms"] % 60000
        or not proof["observed_at_ms"]
        <= proof["as_of_ms"]
        <= proof["finished_at_ms"]
        <= now
        < proof["valid_until_ms"]
        or observed > proof["as_of_ms"]
        or proof.get("digest") != seal(proof)["digest"]
        or (
            expected_control_digest is not None
            and proof.get("control_digest") != expected_control_digest
        )
    ):
        raise ValueError("ENTRY_STRUCTURE_PROOF_INVALID_OR_STALE")
    if expected_controls is not None:
        try:
            values = expected_controls["values"]
            matches = (
                proof["control_digest"] == expected_controls["digest"]
                and proof["control_version"] == expected_controls["version"]
                and proof["mode"] == values["reentry.mode"]
                and proof["enforce_sides"] == values["reentry.enforce_sides"]
                and proof["enforce_reasons"] == values["reentry.enforce_reasons"]
                and proof["policy"] == values["reentry.policy"]
                and proof.get("gate_scope", "CHASE_ONLY")
                == values.get("reentry.gate_scope", "CHASE_ONLY")
                and proof.get("cycle_policy") == values.get("reentry.cycle_policy")
            )
        except (TypeError, KeyError):
            matches = False
        if not matches:
            raise ValueError("ENTRY_STRUCTURE_CONTROL_MISMATCH")
    result = proof.get("assessment")
    gate_scope = proof.get("gate_scope", "CHASE_ONLY")
    if gate_scope not in {"CHASE_ONLY", "TRADE_CYCLE"}:
        raise ValueError("ENTRY_STRUCTURE_GATE_SCOPE_INVALID")
    full_cycle = gate_scope == "TRADE_CYCLE"
    if not isinstance(result, dict) or result.get("status") not in {
        "READY",
        "WAIT",
        "UNKNOWN",
        "NOT_APPLICABLE",
    }:
        raise ValueError("ENTRY_STRUCTURE_ASSESSMENT_INVALID")
    previous = proof.get("previous_exit")
    if result.get("status") in {"READY", "WAIT"} and (
        not isinstance(previous, dict)
        or previous.get("previous_episode") != proof.get("previous_episode")
        or previous.get("side") not in ({"BUY", "SELL"} if full_cycle else {side})
        or type(previous.get("exit_known_ms")) is not int
        or previous["exit_known_ms"] > proof["as_of_ms"]
    ):
        raise ValueError("ENTRY_STRUCTURE_ANCHOR_INVALID")
    if full_cycle:
        from dataclasses import asdict
        from decimal import Decimal, InvalidOperation

        from v2_core.reentry import _PROFIT_EXITS, _UNATTRIBUTED_EXITS, ReentryPolicy
        from v2_core.trade_cycle import (
            LEGACY_VERSION,
            VERSION,
            CyclePolicy,
            opportunity_token,
        )

        try:
            cycle = CyclePolicy(**proof["cycle_policy"])
            policy = ReentryPolicy(**proof["policy"])
            if (
                asdict(cycle) != proof["cycle_policy"]
                or asdict(policy) != proof["policy"]
            ):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise ValueError("ENTRY_STRUCTURE_CYCLE_POLICY_INVALID") from None
        if set(proof.get("enforce_sides", [])) != {"BUY", "SELL"}:
            raise ValueError("ENTRY_STRUCTURE_CYCLE_SIDES_INVALID")
        if result["status"] == "NOT_APPLICABLE" and (
            result.get("reason") != "NO_PREVIOUS_FILLED_EPISODE"
            or previous is not None
            or proof.get("previous_episode") is not None
        ):
            raise ValueError("ENTRY_STRUCTURE_FIRST_ENTRY_INVALID")
        if result["status"] == "READY":
            trigger = result.get("trigger_closed_ms")
            broken = result.get("break_closed_ms")
            width = policy.timeframe_minutes * 60000
            same = previous["side"] == side
            allowed_paths = (
                {"NEW_BREAKOUT", "PULLBACK_RESTART"}
                if same
                else {"REVERSAL_BREAK_CONFIRM"}
            )
            try:
                token_valid = (
                    result["opportunity_token"] == opportunity_token(result)
                    and result["policy"] == proof["policy"]
                    and result["cycle_policy"] == proof["cycle_policy"]
                    and result["policy_digest"]
                    == digest(
                        canonical(
                            {
                                "structure": proof["policy"],
                                "cycle": proof["cycle_policy"],
                            }
                        )
                    )
                )
            except (KeyError, TypeError, ValueError):
                token_valid = False
            if (
                not token_valid
                or result.get("previous_episode") != proof.get("previous_episode")
                or result.get("previous_side") != previous["side"]
                or result.get("side") != side
                or type(trigger) is not int
                or type(broken) is not int
                or not previous["exit_known_ms"]
                < trigger
                <= min(observed, proof["market_cutoff_ms"])
                or trigger % width
                or broken % width
                or not previous["exit_known_ms"] < broken
                or trigger < broken + (cycle.confirmation_bars - 1) * width
                or result.get("confirmation_bars_required") != cycle.confirmation_bars
                or type(result.get("confirmation_bars_observed")) is not int
                or result["confirmation_bars_observed"] < cycle.confirmation_bars
                or (
                    cycle.max_trigger_age_bars > 0
                    and (proof["market_cutoff_ms"] - trigger) // width
                    > cycle.max_trigger_age_bars
                )
                or result.get("version") not in {LEGACY_VERSION, VERSION}
                # v1 never supported repair after a provenance-only external
                # close. Preserve old valid proofs, not a backdated new rule.
                or (
                    previous["exit_reason"] in _UNATTRIBUTED_EXITS
                    and (
                        result.get("version") != VERSION
                        or result.get("unattributed_exit_treatment")
                        != "POST_EXIT_STRUCTURE_REQUIRED"
                    )
                )
                or result.get("execution_authorized") is not False
                or result.get("transition")
                != ("SAME_SIDE_REENTRY" if same else "OPPOSITE_SIDE_REVERSAL")
                or result.get("path") not in allowed_paths
                or result.get("reason")
                != (
                    "NEW_STRUCTURE_CONFIRMED"
                    if same
                    else "REVERSAL_STRUCTURE_CONFIRMED"
                )
            ):
                raise ValueError("ENTRY_STRUCTURE_CYCLE_CONFIRMATION_INVALID")
            if result["path"] == "NEW_BREAKOUT":
                try:
                    net = Decimal(previous["exit_net_pnl"])
                    if (
                        not net.is_finite()
                        or net <= 0
                        or previous["exit_reason"] not in _PROFIT_EXITS
                    ):
                        raise ValueError()
                except (KeyError, ValueError, TypeError, InvalidOperation):
                    raise ValueError(
                        "ENTRY_STRUCTURE_PROFIT_BREAKOUT_INVALID"
                    ) from None
    if (
        result.get("status") in {"READY", "WAIT"}
        and result.get("reason") != "NO_POST_EXIT_CLOSED_BAR"
    ):
        started, finished = (
            proof.get("archive_read_started_ms"),
            proof.get("archive_read_finished_ms"),
        )
        if (
            type(started) is not int
            or type(finished) is not int
            or not proof["observed_at_ms"] <= started <= finished <= proof["as_of_ms"]
            or proof.get("archive_availability_method")
            != "ONLINE_READ_COMPLETED_BEFORE_ASOF"
            or proof.get("historical_replay_compatible") is not False
            or not proof.get("archive_bar_digests")
        ):
            raise ValueError("ENTRY_STRUCTURE_ARCHIVE_EVIDENCE_INVALID")
    reasons, sides = proof.get("enforce_reasons"), proof.get("enforce_sides")
    # This rollout intentionally cannot turn ALL WAIT/UNKNOWN into hard gates.
    if (
        not isinstance(reasons, list)
        or any(reason != "STRUCTURE_CHASE_TOO_FAR" for reason in reasons)
        or not isinstance(sides, list)
        or any(value not in {"BUY", "SELL"} for value in sides)
    ):
        raise ValueError("ENTRY_STRUCTURE_ENFORCEMENT_INVALID")
    if (
        expected_controls is not None
        and now - proof["market_observed_at_ms"] >= values["reentry.max_bar_age_ms"]
    ):
        # A structural-only data gap cannot tighten unrelated base safety gates.
        # This is no veto, not permission to trade or a newly READY assessment.
        return (
            "REENTRY_MARKET_STALE"
            if full_cycle and proof["mode"] == "ENFORCE"
            else None
        )
    if (
        full_cycle
        and proof["mode"] == "ENFORCE"
        and result["status"] in {"WAIT", "UNKNOWN"}
    ):
        reason = result.get("reason")
        if (
            not isinstance(reason, str)
            or not 1 <= len(reason) <= 100
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for c in reason)
        ):
            raise ValueError("ENTRY_STRUCTURE_REASON_INVALID")
        return reason if reason.startswith("REENTRY_") else "REENTRY_" + reason
    if (
        proof["mode"] == "ENFORCE"
        and side in sides
        and result.get("status") == "WAIT"
        and result.get("reason") in reasons
    ):
        return "REENTRY_" + result["reason"]
    return None
