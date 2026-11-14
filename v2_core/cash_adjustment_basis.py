"""Cash-report classification, not a new settlement or rounding authority.

Daily cash already uses venue-reported realized PnL. Historical corrections
that bridge theoretical PnL to that same venue amount must not be counted a
second time. Only immutable, committed evidence identifies such a bridge.
Unrecognized corrections remain unknown instead of disappearing or becoming
invented cash. This SQL is read-only and never changes historical ledger facts.
"""

import re

LEGACY_PRECISION_MODELS = (
    "binance-usdt-per-fill-truncate-8-v1",
    "binance-usdt-observed-entry-truncate-8-v1",
    "venue-reported-boundary-cash-adjustment-v1",
)


def _aliases(adjustment, intent):
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", a) for a in (adjustment, intent)):
        raise ValueError("plain SQL aliases required")


def _source_key_sql(adjustment, intent, source):
    # Ledger.adjustment uses compact JSON, so do not depend on PostgreSQL's
    # different JSON-array whitespace. Each component is still JSON-escaped.
    return (
        "concat('[',to_json("
        + intent
        + ".exchange)::text,',',to_json("
        + intent
        + ".account_id)::text,',',to_json("
        + intent
        + ".environment)::text,',',to_json("
        + intent
        + ".product)::text,',',to_json("
        + adjustment
        + ".kind)::text,',',to_json("
        + source
        + ")::text,']')"
    )


def legacy_bridge_sql(adjustment="c", intent="i"):
    """Recognize only historical directional precision-to-cash bridges.

    This does not try to reconstruct a venue's arithmetic. The correction's
    immutable source identity, amount and proof must agree with its committed
    settlement. Malformed or future model evidence is unclassified, never zero.
    """
    _aliases(adjustment, intent)
    c, i = adjustment, intent
    proof = f"{c}.evidence->'precision'"
    model = f"({proof})->>'model'"
    encoded = f"({proof})->>'amount'"
    models = ",".join("'" + value + "'" for value in LEGACY_PRECISION_MODELS)
    source_key = _source_key_sql(c, i, f"({model}) || ':' || {c}.episode_id::text")
    return f"""(
        {c}.kind='CORRECTION' AND {c}.currency='USDT'
        AND {i}.exchange='BINANCE' AND {i}.environment='SANDBOX'
        AND {i}.product='FUTURES' AND {i}.producer IN ('s6','s8')
        AND {c}.evidence->>'source'='directional-settlement-precision-v1'
        AND ({model}) IN ({models})
        AND {c}.adjustment_key={source_key}
        AND CASE WHEN ({encoded}) ~ '^-?[0-9]{{1,20}}([.][0-9]{{1,18}})?$'
          THEN {c}.amount=({encoded})::numeric ELSE false END
        AND {c}.evidence->>'cash_audit_digest' ~ '^[0-9a-f]{{64}}$'
        AND {c}.evidence->>'income_digest' ~ '^[0-9a-f]{{64}}$'
        AND {c}.evidence->>'final_inventory'
          ~ '^[0-9a-f]{{8}}(-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}$'
        AND EXISTS (
          SELECT 1 FROM v2_settlements basis_settlement
          WHERE basis_settlement.episode_id={c}.episode_id
            AND basis_settlement.currency={c}.currency
            AND basis_settlement.evidence->>'source'='directional-settlement-v1'
            AND basis_settlement.evidence->'precision_reconciliation'={proof}
            AND basis_settlement.evidence->>'cash_audit_digest'
              ={c}.evidence->>'cash_audit_digest'
            AND basis_settlement.evidence->>'final_inventory'
              ={c}.evidence->>'final_inventory'
            AND basis_settlement.evidence @> '{{"exchange_flat":true,
              "orders_terminal":true,"fills_complete":true,"cash_complete":true}}'::jsonb
        )
    )"""


def cash_adjustment_basis_sql(adjustment="c", intent="i"):
    """Return REAL_CASH, THEORY_TO_VENUE_BRIDGE or UNKNOWN for one row."""
    _aliases(adjustment, intent)
    c, i = adjustment, intent
    bridge = legacy_bridge_sql(c, i)
    insurance_source = f"'insurance-clear:' || ({c}.evidence->>'income_id')"
    insurance_key = _source_key_sql(c, i, insurance_source)
    return f"""CASE
        WHEN {bridge} THEN 'THEORY_TO_VENUE_BRIDGE'
        WHEN {c}.kind='FUNDING' THEN 'REAL_CASH'
        WHEN {c}.kind='CORRECTION'
          AND {c}.evidence->>'source'='verified-liquidation-insurance-clear-v1'
          AND {c}.evidence->>'income_type'='INSURANCE_CLEAR'
          AND {c}.adjustment_key={insurance_key}
          AND EXISTS (
            SELECT 1 FROM v2_exchange_income basis_income
            WHERE basis_income.income_id::text={c}.evidence->>'income_id'
              AND (basis_income.exchange,basis_income.account_id,
                   basis_income.environment,basis_income.product)
                =({i}.exchange,{i}.account_id,{i}.environment,{i}.product)
              AND basis_income.symbol={i}.payload->>'symbol'
              AND basis_income.income_type='INSURANCE_CLEAR'
              AND basis_income.amount={c}.amount
              AND basis_income.currency={c}.currency
              AND basis_income.occurred_at_ms={c}.occurred_at_ms
              AND basis_income.evidence='{{"source":"binance-income","trade_id":""}}'::jsonb
          ) THEN 'REAL_CASH'
        ELSE 'UNKNOWN' END"""
