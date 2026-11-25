"""Bounded accumulation projections, not full archived research reports.

SQL projects before transfer/JSON decoding. The latest revision is selected
before any validation: unreadable latest evidence cannot fall back to an older
successful revision, nor silently disappear from the history coverage result.
"""

import json
from datetime import date, datetime, time, timedelta, timezone

HISTORY_DAYS = 30
MAX_DAY_BYTES = 1024 * 1024
MAX_HISTORY_BYTES = 8 * 1024 * 1024
ERROR_KEY = "history_read_error"
_ERRORS = frozenset(
    {
        "HISTORY_DAY_TOO_LARGE",
        "HISTORY_TOTAL_TOO_LARGE",
        "HISTORY_PROJECTION_INVALID",
    }
)


def _object(expression, fields):
    """SQL from fixed source-code expressions only; never caller input."""
    names = ",".join("'" + name + "'" for name in fields)
    branches = " ".join(
        "WHEN '" + name + "' THEN " + value for name, value in fields.items()
    )
    return (
        f"CASE WHEN jsonb_typeof({expression})='object' THEN "
        "(SELECT COALESCE(jsonb_object_agg(k,CASE k "
        + branches
        + " END),'{}'::jsonb) FROM jsonb_each("
        + expression
        + f") AS projected(k,v) WHERE k IN ({names})) "
        + f"ELSE {expression} END"
    )


def _array(expression, item, maximum):
    # Bad shape remains bad, rather than being normalized into an empty sample.
    return (
        f"CASE WHEN jsonb_typeof({expression})='array' THEN "
        f"CASE WHEN jsonb_array_length({expression})<={maximum} THEN "
        f"(SELECT COALESCE(jsonb_agg({item} ORDER BY ordinal),'[]'::jsonb) "
        f"FROM jsonb_array_elements({expression}) WITH ORDINALITY AS items(item,ordinal)) "
        f"ELSE 'null'::jsonb END ELSE {expression} END"
    )


_METRIC_FIELDS = (
    "return_pct",
    "range_pct_of_open",
    "close_location_in_range_pct",
)
_SAMPLE = _object("item", {name: "v" for name in _METRIC_FIELDS})
_COMPARISON = _object(
    "v",
    {
        "selected_samples": _array("v", _SAMPLE, 3000),
        "control_samples": _array("v", _SAMPLE, 3000),
    },
)
_REASON = _object("item", {"evidence": _object("v", {"primary_reason": "v"})})
_PROJECTED_REPORT = _object(
    "report",
    {
        "version": "v",
        "window": _object("v", {"start_ms": "v"}),
        "as_of_ms": "v",
        "market_source": "v",
        "scope_key": "v",
        "market_digest": "v",
        "market_comparison": _COMPARISON,
        "rows": _array("v", _REASON, 3000),
    },
)

HISTORY_SQL = f"""WITH latest AS MATERIALIZED (
    SELECT DISTINCT ON(day) day,report FROM v2_daily_mover_reports
    WHERE scope_key=%s AND method_version=%s AND day<%s::date
      AND day>=%s::date-30
    ORDER BY day DESC,created_at DESC,report_id DESC LIMIT 30
), projections AS MATERIALIZED (
    SELECT day,({_PROJECTED_REPORT})::text AS payload FROM latest
), sized AS MATERIALIZED (
    SELECT day,payload,octet_length(payload) AS size FROM projections
), bounded AS (
    SELECT *,sum(CASE WHEN size<=%s THEN size ELSE 0 END)
        OVER(ORDER BY day DESC ROWS UNBOUNDED PRECEDING) AS running_bytes
    FROM sized
)
SELECT day,size,
    CASE WHEN size<=%s AND running_bytes<=%s THEN payload ELSE NULL END,
    CASE WHEN size>%s THEN 'HISTORY_DAY_TOO_LARGE'
         WHEN running_bytes>%s THEN 'HISTORY_TOTAL_TOO_LARGE' ELSE NULL END
FROM bounded ORDER BY day DESC"""


def history_error(day, reason):
    if not isinstance(reason, str) or reason not in _ERRORS:
        reason = "HISTORY_PROJECTION_INVALID"
    return {"history_day": day.isoformat(), ERROR_KEY: reason}


def decode_history(rows, day):
    """Validate bounded wire payloads before decoding any historical objects."""
    target = date.fromisoformat(day)
    if target.isoformat() != day:
        raise ValueError("MOVER_HISTORY_DAY_INVALID")
    result, total, seen = [], 0, set()
    for stored_day, size, encoded, error in rows:
        if (
            type(stored_day) is not date
            or not target - timedelta(days=HISTORY_DAYS) <= stored_day < target
            or stored_day in seen
            or len(result) >= HISTORY_DAYS
            or type(size) is not int
            or size < 0
        ):
            raise ValueError("MOVER_HISTORY_ROW_INVALID")
        seen.add(stored_day)
        if error is not None:
            result.append(history_error(stored_day, error))
            continue
        if size > MAX_DAY_BYTES:
            result.append(history_error(stored_day, "HISTORY_DAY_TOO_LARGE"))
            continue
        total += size
        if total > MAX_HISTORY_BYTES:
            result.append(history_error(stored_day, "HISTORY_TOTAL_TOO_LARGE"))
            continue
        if not isinstance(encoded, str) or len(encoded.encode()) != size:
            result.append(history_error(stored_day, "HISTORY_PROJECTION_INVALID"))
            continue
        try:
            payload = json.loads(encoded)
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            result.append(history_error(stored_day, "HISTORY_PROJECTION_INVALID"))
        else:
            window = payload.get("window")
            start = window.get("start_ms") if isinstance(window, dict) else None
            expected_start = (
                int(
                    datetime.combine(
                        stored_day, time(), timezone(timedelta(hours=8))
                    ).timestamp()
                )
                * 1000
            )
            if (
                payload.get("version") == "daily-mover-report-v1"
                and type(start) is int
                and start != expected_start
            ):
                result.append(history_error(stored_day, "HISTORY_PROJECTION_INVALID"))
            else:
                result.append(payload)
    return result
