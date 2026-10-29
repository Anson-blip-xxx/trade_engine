"""Bounded public-mainnet research, never an order producer or live consumer.

No API keys, account reads, local state files, database writes or live imports.
The current catalog is NOT a historical universe. Results are exploratory even
when every requested bar/funding page is returned. Run as a separate offline job.
"""

import argparse
import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import datetime
from http.client import HTTPException, HTTPSConnection
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from v2_core.evidence import canonical, digest
from v2_core.long_trend import LongTrendPolicy, replay
from v2_core.public_market import unique_object
from v2_core.quant_indicators import Bar, decimal

HOUR = 3_600_000
DAY = 24 * HOUR
VERSION = "long-trend-public-research-v1"


class ResearchDataError(ValueError):
    """Only fixed diagnostic codes cross the reporting boundary."""


def validate_symbol(target):
    # Mainnet has CJK asset names. This public-only reader must not make the
    # entire catalog unusable by imposing the execution layer's ASCII contract.
    if (
        not isinstance(target, str)
        or not 5 <= len(target) <= 40
        or not target.endswith("USDT")
        or not target[:-4].isalnum()
    ):
        raise ResearchDataError("RESEARCH_SYMBOL_INVALID")
    return target


class PublicResearchTransport:
    """GET allowlist, no redirect/auth support, finite one-shot request budget.

    This is not a distributed rate limiter: concurrent scheduled jobs must not
    use it until a shared quota/lease is wired. A 418/429 stops the entire job.
    """

    def __init__(self, *, max_requests=100, max_weight=250, deadline_seconds=240):
        if any(
            type(x) is not int for x in (max_requests, max_weight, deadline_seconds)
        ):
            raise ValueError("RESEARCH_TRANSPORT_BOUNDS")
        if not (
            1 <= max_requests <= 100
            and 1 <= max_weight <= 250
            and 1 <= deadline_seconds <= 300
        ):
            raise ValueError("RESEARCH_TRANSPORT_BOUNDS")
        self.requests = self.weight = 0
        self.max_requests, self.max_weight = max_requests, max_weight
        self.deadline = time.monotonic() + deadline_seconds
        self.blocked = False

    def __call__(self, path, params):
        if self.blocked:
            raise ResearchDataError("RESEARCH_JOB_STOPPED")
        if path in {"/fapi/v1/exchangeInfo", "/fapi/v1/time"} and params == {}:
            weight = 1
        elif path in {"/fapi/v1/klines", "/fapi/v1/fundingRate"}:
            keys = {"symbol", "startTime", "endTime", "limit"}
            if path.endswith("klines"):
                keys.add("interval")
            if (
                not isinstance(params, dict)
                or set(params) != keys
                or params["limit"] != 1000
                or type(params["limit"]) is not int
                or path.endswith("klines")
                and params["interval"] != "1h"
            ):
                raise ResearchDataError("RESEARCH_ENDPOINT_DENIED")
            validate_symbol(params["symbol"])
            if (
                any(type(params[k]) is not int for k in ("startTime", "endTime"))
                or not 0 <= params["startTime"] <= params["endTime"]
                or params["endTime"] - params["startTime"] > 180 * DAY
            ):
                raise ResearchDataError("RESEARCH_REQUEST_RANGE")
            weight = 5 if path.endswith("klines") else 1
        else:
            raise ResearchDataError("RESEARCH_ENDPOINT_DENIED")
        if (
            self.requests >= self.max_requests
            or self.weight + weight > self.max_weight
            or time.monotonic() >= self.deadline
        ):
            self.blocked = True
            raise ResearchDataError("RESEARCH_REQUEST_BUDGET")
        self.requests += 1
        self.weight += weight
        conn = HTTPSConnection("fapi.binance.com", timeout=5)
        try:
            conn.request(
                "GET",
                path + ("?" + urlencode(params) if params else ""),
                headers={"Accept": "application/json"},
            )
            response = conn.getresponse()
            if response.status in (418, 429):
                self.blocked = True
                raise ResearchDataError("RESEARCH_RATE_LIMITED")
            if response.status != 200:
                raise ResearchDataError("RESEARCH_HTTP_ERROR")
            # Leave ample shared IP headroom; no busy retries after this guard.
            used = response.getheader("X-MBX-USED-WEIGHT-1M", "0")
            if used.isdigit() and int(used) >= 1000:
                self.blocked = True
                raise ResearchDataError("RESEARCH_SHARED_IP_BUSY")
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ResearchDataError("RESEARCH_RESPONSE_TOO_LARGE")
            result = json.loads(
                body,
                object_pairs_hook=unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if (
                not isinstance(result, (dict, list))
                or isinstance(result, dict)
                and "code" in result
            ):
                raise ResearchDataError("RESEARCH_API_ERROR")
            return result
        except ResearchDataError:
            raise
        except (HTTPException, OSError, ValueError):
            raise ResearchDataError("RESEARCH_TRANSPORT_FAILURE") from None
        finally:
            conn.close()


def catalog_symbols(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("symbols"), list):
        raise ResearchDataError("RESEARCH_CATALOG_INVALID")
    result = {}
    for row in catalog["symbols"]:
        if not isinstance(row, dict):
            raise ResearchDataError("RESEARCH_CATALOG_INVALID")
        if (
            row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
        ):
            target = validate_symbol(row.get("symbol"))
            if target in result:
                raise ResearchDataError("RESEARCH_CATALOG_DUPLICATE")
            result[target] = row
    return result


def select_cohort(catalog, *, required=(), count=12, listed_before_ms):
    """Reproducible hash controls, NOT performance-ranked or unbiased universe."""
    eligible = catalog_symbols(catalog)
    if (
        type(count) is not int
        or not 1 <= count <= 16
        or not isinstance(required, (tuple, list))
        or len(set(required)) != len(required)
        or len(required) > count
        or type(listed_before_ms) is not int
    ):
        raise ValueError("RESEARCH_COHORT_BOUND")
    for target in required:
        validate_symbol(target)
        if target not in eligible:
            raise ResearchDataError("RESEARCH_REQUIRED_SYMBOL_UNAVAILABLE")
    controls = [
        target
        for target, row in eligible.items()
        if target not in required
        and type(row.get("onboardDate")) is int
        and row["onboardDate"] <= listed_before_ms
    ]
    controls.sort(
        key=lambda s: hashlib.sha256((VERSION + ":" + s).encode()).hexdigest()
    )
    return tuple(required) + tuple(controls[: count - len(required)])


def read_history(public, target, *, start_ms, end_ms, now_ms):
    validate_symbol(target)
    if (
        any(type(t) is not int for t in (start_ms, end_ms, now_ms))
        or not 0 <= start_ms < end_ms <= now_ms
        or start_ms % HOUR
        or end_ms % HOUR
        or end_ms - start_ms > 180 * DAY
    ):
        raise ValueError("RESEARCH_HISTORY_BOUND")
    bars, cursor = [], start_ms
    while cursor < end_ms:
        rows = public(
            "/fapi/v1/klines",
            {
                "symbol": target,
                "interval": "1h",
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": 1000,
            },
        )
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ResearchDataError("RESEARCH_HISTORY_INCOMPLETE")
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) < 7
                or type(row[0]) is not int
                or row[0] != cursor
                or type(row[6]) is not int
                or row[6] != cursor + HOUR - 1
                or cursor + HOUR > end_ms
            ):
                raise ResearchDataError("RESEARCH_HISTORY_GAP_OR_UNCLOSED")
            bars.append(Bar(row[0], *row[1:6]))
            cursor += HOUR
    events, cursor, pages = [], start_ms, 0
    while cursor < end_ms:
        pages += 1
        if pages > 10:
            raise ResearchDataError("RESEARCH_FUNDING_PAGE_BOUND")
        rows = public(
            "/fapi/v1/fundingRate",
            {
                "symbol": target,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": 1000,
            },
        )
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ResearchDataError("RESEARCH_FUNDING_INVALID")
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("symbol") != target
                or type(row.get("fundingTime")) is not int
                or not cursor <= row["fundingTime"] < end_ms
                or "fundingRate" not in row
                or "markPrice" not in row
                or abs(decimal(row["fundingRate"])) > 1
                or decimal(row["markPrice"]) <= 0
            ):
                raise ResearchDataError("RESEARCH_FUNDING_INVALID")
            events.append(
                {
                    "at_ms": row["fundingTime"],
                    "rate": row["fundingRate"],
                    "mark_price": row["markPrice"],
                }
            )
            cursor = row["fundingTime"] + 1
        if len(rows) < 1000:
            break
    return (
        tuple(bars),
        tuple(events),
        {
            "bars": len(bars),
            "funding_events": len(events),
            "funding_pages": pages,
            "funding_history_fetch_complete": True,
            "historical_funding_schedule_verified": False,
            "digest": digest(
                canonical({"bars": [b.payload() for b in bars], "funding": events})
            ),
        },
    )


def venue_policy(contract, base):
    """Use today's filters as declared assumptions, never historical truth."""
    try:
        if not isinstance(contract, dict) or not isinstance(
            contract.get("filters"), list
        ):
            raise TypeError("invalid filter collection")
        filters = {}
        for item in contract["filters"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("filterType"), str)
                or item["filterType"] in filters
            ):
                raise ValueError("invalid or duplicate filter")
            filters[item["filterType"]] = item
        lot = filters["LOT_SIZE"]
        market = filters.get("MARKET_LOT_SIZE", lot)
        step = max(decimal(lot["stepSize"]), decimal(market["stepSize"]))
        smaller = min(decimal(lot["stepSize"]), decimal(market["stepSize"]))
        if smaller < 0 or step <= 0:
            raise ValueError("invalid quantity step")
        if smaller > 0 and step % smaller:
            raise ValueError("non-nested steps")
        return replace(
            base,
            price_tick=filters["PRICE_FILTER"]["tickSize"],
            quantity_step=str(step),
            minimum_quantity=str(
                max(decimal(lot["minQty"]), decimal(market["minQty"]))
            ),
            maximum_quantity=str(
                min(decimal(lot["maxQty"]), decimal(market["maxQty"]))
            ),
            minimum_notional=filters["MIN_NOTIONAL"]["notional"],
        )
    except (KeyError, TypeError, ValueError):
        raise ResearchDataError("RESEARCH_VENUE_FILTERS") from None


def study(public, *, catalog, symbols, start_ms, end_ms, now_ms, policy=None):
    """Two frozen-policy periods; second is temporal diagnostic, not true OOS."""
    policy = policy or LongTrendPolicy()
    contracts = catalog_symbols(catalog)
    if (
        not isinstance(symbols, (tuple, list))
        or not 1 <= len(symbols) <= 16
        or len(set(symbols)) != len(symbols)
        or any(type(t) is not int for t in (start_ms, end_ms, now_ms))
        or start_ms % DAY
        or end_ms % HOUR
        or not 30 * DAY <= end_ms - start_ms <= 120 * DAY
        or end_ms > now_ms
    ):
        raise ValueError("RESEARCH_STUDY_BOUND")
    warmup = max(
        policy.breakout_days + 1, policy.atr_days + 1, policy.structure_days + 1
    )
    history_start = start_ms - warmup * DAY
    if history_start < 0 or end_ms - history_start > 180 * DAY:
        raise ValueError("RESEARCH_WARMUP_BOUND")
    split_ms = start_ms + ((end_ms - start_ms) // DAY * 2 // 3) * DAY
    reports = []
    for target in symbols:
        validate_symbol(target)
        if target not in contracts:
            reports.append(
                {"symbol": target, "status": "NOT_IN_CURRENT_MAINNET_CATALOG"}
            )
            continue
        try:
            current_policy = venue_policy(contracts[target], policy)
            bars, events, coverage = read_history(
                public, target, start_ms=history_start, end_ms=end_ms, now_ms=now_ms
            )
            folds = {}
            for label, entry_start, stop in (
                ("initial", start_ms, split_ms),
                ("later_reset", split_ms, end_ms),
                ("full", start_ms, end_ms),
            ):
                selected = tuple(b for b in bars if b.t < stop)
                result = replay(
                    selected,
                    policy=current_policy,
                    funding=tuple(e for e in events if e["at_ms"] < stop),
                    funding_complete=False,
                    entry_start_ms=entry_start,
                    closed_at_ms=stop,
                )
                folds[label] = {
                    k: v for k, v in result.items() if k not in {"curve", "decisions"}
                }
            reports.append(
                {
                    "symbol": target,
                    "status": "SIMULATED",
                    "coverage": coverage,
                    "folds": folds,
                }
            )
        except (ResearchDataError, ValueError) as exc:
            code = str(exc)
            if not code.isupper() or len(code) > 100:
                code = "RESEARCH_INPUT_INVALID"
            reports.append({"symbol": target, "status": "UNCOVERED", "reason": code})
            if getattr(public, "blocked", False):
                break
    return {
        "version": VERSION,
        "execution_authorized": False,
        "profitability_valid": False,
        "market_environment": "LIVE_PUBLIC_ONLY",
        "execution_environment": "NONE",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "split_ms": split_ms,
        "asof_utc8": datetime.fromtimestamp(
            now_ms / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "base_policy": asdict(policy),
        "requested_symbols": list(symbols),
        "reports": reports,
        "unprocessed_symbols": list(symbols[len(reports) :]),
        "coverage_complete": len(reports) == len(symbols)
        and all(row["status"] == "SIMULATED" for row in reports),
        "assumptions": [
            "CURRENT_CATALOG_SURVIVORSHIP_BIAS",
            "CURRENT_NOT_HISTORICAL_VENUE_FILTERS",
            "LATER_PERIOD_NOT_TRUE_UNSEEN_HOLDOUT",
            "NO_PARAMETERS_FITTED_BY_THIS_JOB",
            "PER_SYMBOL_CAPITAL_IS_NOT_AN_AGGREGATE_PORTFOLIO",
            "HISTORICAL_FUNDING_SCHEDULE_NOT_VERIFIED",
            "NO_PERSISTENCE_OR_EXECUTION",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start-ms", type=int, required=True, help="UTC day aligned entry start"
    )
    parser.add_argument(
        "--end-ms", type=int, help="exclusive closed hour; default server time floor"
    )
    parser.add_argument(
        "--required", default="BTWUSDT,BTCUSDT,ETHUSDT", help="named case and controls"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=12,
        help="1–16, hash-selected current-universe controls",
    )
    parser.add_argument(
        "--policy", default="{}", help="JSON research-only policy overrides"
    )
    args = parser.parse_args()
    policy = LongTrendPolicy(**json.loads(args.policy))
    public = PublicResearchTransport()
    now = public("/fapi/v1/time", {})["serverTime"]
    catalog = public("/fapi/v1/exchangeInfo", {})
    symbols = select_cohort(
        catalog,
        required=tuple(args.required.split(",")),
        count=args.count,
        listed_before_ms=args.start_ms - 60 * DAY,
    )
    report = study(
        public,
        catalog=catalog,
        symbols=symbols,
        start_ms=args.start_ms,
        end_ms=args.end_ms or now // HOUR * HOUR,
        now_ms=now,
        policy=policy,
    )
    report["cohort_selection"] = "NAMED_CASES_PLUS_VERSIONED_SHA256_CURRENT_SURVIVORS"
    report["requests"] = public.requests
    report["request_weight"] = public.weight
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
