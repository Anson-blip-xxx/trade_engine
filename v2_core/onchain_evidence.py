"""Descriptors of optional DEX provider aggregates, never trading authority.

An aggregate's provider update time is not a swap or finalized block time.
Missing data remains UNKNOWN. Address identity comes from reviewed mappings,
not ticker searches, and separate overlapping windows must never be summed.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID

from v2_core.evidence import canonical, digest

VERSION = "onchain-public-observation-v1"
SOURCE = "DEXPAPRIKA_PUBLIC_KEYLESS"
PROVIDER = "DEXPAPRIKA"
WINDOWS = ("1m", "5m", "15m", "1h")
AUTH_FIELDS = (
    "execution_authorized",
    "automatic_parameter_updates",
    "promotion_authorized",
)
MAPPING_FIELDS = frozenset(
    {
        "version",
        "provider",
        "symbol",
        "network_id",
        "token_address",
        "contract_multiplier",
        "status",
        "verified_at_ms",
        "valid_until_ms",
        "source_links",
        "reason_code",
    }
)
LIMITATIONS = [
    "RESEARCH_ONLY_NO_EXECUTION_AUTHORITY",
    "PROVIDER_AGGREGATES_NOT_INDEPENDENTLY_RECONSTRUCTED_SWAPS",
    "PROVIDER_UPDATED_TIME_IS_NOT_SWAP_EVENT_OR_BLOCK_TIME",
    "OVERLAPPING_WINDOWS_ARE_INDEPENDENT_DO_NOT_SUM",
    "DEX_COVERAGE_IS_NOT_TOTAL_MARKET_OR_BINANCE_INTERNAL_FLOW",
    "USD_LIQUIDITY_CHANGE_IS_NOT_PROOF_OF_LP_WITHDRAWAL",
    "POOL_PRICE_IS_NOT_ASSUMED_TO_BE_TARGET_TOKEN_PRICE",
    "NO_HISTORICAL_AVAILABILITY_OR_FINALITY_PROOF",
    "NO_ADDRESS_OWNER_OR_EXCHANGE_WALLET_ATTRIBUTION",
]


def _ms(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("ONCHAIN_TIMESTAMP_INVALID")
    return value


def _network(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", value
    ):
        raise ValueError("ONCHAIN_NETWORK_INVALID")
    # Provider network identifiers remain explicit; no unreviewed alias lookup.
    return value.lower()


def _address(value, network):
    if not isinstance(value, str):
        raise ValueError("ONCHAIN_ADDRESS_INVALID")  # noqa: TRY004 - stable research error code
    if network in {
        "bsc",
        "ethereum",
        "base",
        "arbitrum",
        "polygon",
        "optimism",
        "avalanche",
    }:
        if not re.fullmatch(r"0[xX][0-9a-fA-F]{40}", value):
            raise ValueError("ONCHAIN_ADDRESS_INVALID")
        return value.lower()
    if not re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value):
        raise ValueError("ONCHAIN_ADDRESS_INVALID")
    # Non-EVM addresses may be case-sensitive.
    return value


def _number(value, *, nonnegative=True):
    if type(value) not in (str, int) or len(str(value)) > 80:
        raise ValueError("ONCHAIN_NUMBER_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("ONCHAIN_NUMBER_INVALID") from None
    if (
        not number.is_finite()
        or abs(number) > Decimal("1e40")
        or (number and number.adjusted() < -40)
        or (nonnegative and number < 0)
    ):
        raise ValueError("ONCHAIN_NUMBER_INVALID")
    return number


def _text(value):
    return "0" if value == 0 else format(value.normalize(), "f")


def _integer(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError("ONCHAIN_COUNT_INVALID")
    return value


def _iso_ms(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
    ):
        raise ValueError("ONCHAIN_PROVIDER_TIME_INVALID")
    try:
        delta = datetime.fromisoformat(value[:-1] + "+00:00") - datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
    except ValueError:
        raise ValueError("ONCHAIN_PROVIDER_TIME_INVALID") from None
    return _ms(
        delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
    )


def _unknown(reason):
    return {"status": "UNKNOWN", "reason": reason}


def _fingerprint(payload):
    try:
        encoded = canonical({"payload": payload})
    except (ValueError, TypeError, RecursionError):
        raise ValueError("ONCHAIN_PAYLOAD_INVALID") from None
    if len(encoded.encode("utf-8")) > 262_144:
        raise ValueError("ONCHAIN_PAYLOAD_BOUND")
    return digest(encoded)


def _scope(bundle, mapping, as_of_ms, max_age_ms):
    _ms(as_of_ms)
    if type(max_age_ms) is not int or not 1 <= max_age_ms <= 3_600_000:
        raise ValueError("ONCHAIN_AGE_POLICY_INVALID")
    if not isinstance(bundle, dict) or not isinstance(mapping, dict):
        raise ValueError("ONCHAIN_MAPPING_UNAVAILABLE")  # noqa: TRY004 - stable research error code
    if (
        bundle.get("version") != VERSION
        or bundle.get("source") != SOURCE
        or bundle.get("provider") != PROVIDER
        or bundle.get("source_host", "api.dexpaprika.com") != "api.dexpaprika.com"
    ):
        raise ValueError("ONCHAIN_SOURCE_INVALID")
    if any(bundle.get(key) is not False for key in AUTH_FIELDS):
        raise ValueError("ONCHAIN_RESEARCH_ONLY_REQUIRED")
    if (
        mapping.get("version") != "onchain-asset-map-v1"
        or mapping.get("provider") != PROVIDER
        or mapping.get("status") != "VERIFIED"
    ):
        raise ValueError("ONCHAIN_MAPPING_UNVERIFIED")
    start, finish = _ms(bundle.get("started_at_ms")), _ms(bundle.get("finished_at_ms"))
    if not start <= finish <= as_of_ms or as_of_ms - finish > max_age_ms:
        raise ValueError("ONCHAIN_RECEIPT_NOT_CURRENT")
    verified, received = (
        _ms(mapping.get("verified_at_ms")),
        _ms(mapping.get("first_received_at_ms")),
    )
    expiry = _ms(mapping.get("valid_until_ms"))
    if not verified <= received <= start <= as_of_ms < expiry:
        raise ValueError("ONCHAIN_MAPPING_NOT_AVAILABLE")
    if not MAPPING_FIELDS <= mapping.keys():
        raise ValueError("ONCHAIN_MAPPING_INCOMPLETE")
    try:
        expected = digest(canonical({key: mapping[key] for key in MAPPING_FIELDS}))
        mapping_id = str(UUID(mapping.get("mapping_id")))
        bundle_id = str(UUID(bundle.get("mapping_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("ONCHAIN_MAPPING_INVALID") from None
    if mapping.get("input_digest") != expected:
        raise ValueError("ONCHAIN_MAPPING_DIGEST_MISMATCH")
    if mapping_id != bundle_id or mapping.get("symbol") != bundle.get("symbol"):
        raise ValueError("ONCHAIN_MAPPING_IDENTITY_MISMATCH")
    if not isinstance(bundle.get("symbol"), str) or not re.fullmatch(
        r"[A-Z0-9_]{1,40}USDT", bundle["symbol"]
    ):
        raise ValueError("ONCHAIN_SYMBOL_INVALID")
    network = _network(mapping.get("network_id"))
    address = _address(mapping.get("token_address"), network)
    if (
        _network(bundle.get("network_id")) != network
        or _address(bundle.get("token_address"), network) != address
    ):
        raise ValueError("ONCHAIN_MAPPING_IDENTITY_MISMATCH")
    multiplier = _number(mapping["contract_multiplier"])
    if not 0 < multiplier <= 1_000_000 or multiplier != multiplier.to_integral_value():
        raise ValueError("ONCHAIN_MULTIPLIER_INVALID")
    return network, address, start, finish


def _component(bundle, name, start, finish, as_of_ms, max_age_ms, network, address):
    components = bundle.get("components")
    if not isinstance(components, dict) or name not in components:
        raise ValueError("ONCHAIN_COMPONENT_MISSING")
    component = components[name]
    if not isinstance(component, dict):
        raise ValueError("ONCHAIN_COMPONENT_INVALID")  # noqa: TRY004 - stable research error code
    receipt = _ms(component.get("fetched_at_ms"))
    if not start <= receipt <= finish <= as_of_ms or as_of_ms - receipt > max_age_ms:
        raise ValueError("ONCHAIN_COMPONENT_RECEIPT_NOT_CURRENT")
    expected_endpoint = (
        f"/networks/{network}/tokens/{address}"
        if name == "token"
        else f"/networks/{network}/pools/search"
    )
    if component.get("endpoint") != expected_endpoint:
        raise ValueError("ONCHAIN_ENDPOINT_MISMATCH")
    if component.get("status") == "UNKNOWN":
        reason = component.get("reason")
        if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", reason):
            raise ValueError(reason)
        raise ValueError("ONCHAIN_COMPONENT_UNAVAILABLE")
    if component.get("status") != "OBSERVED" or component.get("reason") is not None:
        raise ValueError("ONCHAIN_COMPONENT_INVALID")
    payload = component.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("ONCHAIN_PAYLOAD_INVALID")  # noqa: TRY004 - stable research error code
    if component.get("sha256") != _fingerprint(payload):
        raise ValueError("ONCHAIN_COMPONENT_DIGEST_MISMATCH")
    return payload, receipt


def _assert_identity(payload, network, address):
    if (
        _network(payload.get("chain")) != network
        or _address(payload.get("id"), network) != address
    ):
        raise ValueError("ONCHAIN_PROVIDER_IDENTITY_MISMATCH")


def _aggregate(
    payload, mapping, receipt, as_of_ms, max_age_ms, network, address, missing
):
    _assert_identity(payload, network, address)
    if payload.get("no_index") is not False:
        raise ValueError("ONCHAIN_TOKEN_INDEX_UNCONFIRMED")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("ONCHAIN_SUMMARY_MISSING")  # noqa: TRY004 - stable research error code
    _assert_identity(summary, network, address)
    provider_symbol = payload.get("symbol")
    if not isinstance(provider_symbol, str) or not re.fullmatch(
        r"[A-Za-z0-9_]{1,40}", provider_symbol
    ):
        raise ValueError("ONCHAIN_PROVIDER_SYMBOL_INVALID")
    # Address identity was checked first. A known 1:1 asset's contradictory
    # ticker is rejected, never used to silently discover a replacement token.
    if (
        Decimal(mapping["contract_multiplier"]) == 1
        and provider_symbol.upper() != mapping["symbol"][:-4]
    ):
        raise ValueError("ONCHAIN_PROVIDER_SYMBOL_MISMATCH")
    updated = _iso_ms(payload.get("last_updated"))
    if not updated <= receipt <= as_of_ms or as_of_ms - updated > max_age_ms:
        raise ValueError("ONCHAIN_PROVIDER_UPDATE_NOT_CURRENT")
    result = {
        "status": "OBSERVED_AGGREGATE",
        "provider_symbol": provider_symbol,
        "provider_updated_at_ms": updated,
        "provider_update_age_ms": as_of_ms - updated,
        "received_at_ms": receipt,
        "freshness": "PROVIDER_UPDATE_ONLY",
        "event_time_ms": None,
        "historical_available": False,
        "windows": {},
    }
    for field in ("price_usd", "liquidity_usd", "fdv", "pools"):
        if field not in summary:
            result[field] = None
            missing.add(f"token.summary.{field}")
        else:
            result[field] = (
                _integer(summary[field])
                if field == "pools"
                else _text(_number(summary[field]))
            )
    for window in WINDOWS:
        raw = summary.get(window)
        if not isinstance(raw, dict):
            result["windows"][window] = _unknown("ONCHAIN_WINDOW_MISSING")
            missing.add(f"token.summary.{window}")
            continue
        try:
            if "buy_usd" not in raw or "sell_usd" not in raw:
                raise ValueError("ONCHAIN_BUY_SELL_AMOUNT_MISSING")
            buy, sell = _number(raw["buy_usd"]), _number(raw["sell_usd"])
            total = buy + sell
            item = {
                "status": "OBSERVED_AGGREGATE",
                "buy_usd": _text(buy),
                "sell_usd": _text(sell),
                "signed_amount_imbalance": _text((buy - sell) / total)
                if total
                else None,
                "imbalance_reason": "ZERO_REPORTED_AMOUNT" if not total else None,
                "basis": "PROVIDER_AGGREGATE_NOT_WALLET_NET_FLOW",
                "window_ends_at_event_ms": None,
            }
            for field in ("volume_usd", "buys", "sells", "txns"):
                if field not in raw:
                    item[field] = None
                    missing.add(f"token.summary.{window}.{field}")
                else:
                    item[field] = (
                        _text(_number(raw[field]))
                        if field == "volume_usd"
                        else _integer(raw[field])
                    )
            result["windows"][window] = item
        except ValueError as exc:
            result["windows"][window] = _unknown(str(exc))
            missing.add(f"token.summary.{window}.buy_sell_amounts")
    if any(x["status"] == "UNKNOWN" for x in result["windows"].values()):
        result["status"] = "PARTIAL"
    return result


def _pool_identity(row, network):
    """Pool identifiers are not necessarily token/contract addresses.

    Uniswap v4 uses keccak256(PoolKey), a bytes32 identifier inside a singleton.
    https://developers.uniswap.org/docs/ecosystem/subgraphs/concepts/v4/entities
    Only this documented provider DEX identifier permits that representation;
    unknown DEX types must not silently inherit the exception. Token identity
    always remains governed by the independent strict _address validator.
    """
    value, dex_id = row.get("id"), row.get("dex_id")
    if dex_id is not None and (
        not isinstance(dex_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", dex_id)
    ):
        raise ValueError("ONCHAIN_POOL_DEX_INVALID")
    if dex_id == "uniswap_v4":
        if (
            network
            not in {
                "bsc",
                "ethereum",
                "base",
                "arbitrum",
                "polygon",
                "optimism",
                "avalanche",
            }
            or not isinstance(value, str)
            or not re.fullmatch(r"0[xX][0-9a-fA-F]{64}", value)
        ):
            raise ValueError("ONCHAIN_V4_POOL_ID_INVALID")
        return value.lower(), None, "BYTES32_POOL_ID", dex_id
    if isinstance(value, str) and re.fullmatch(r"0[xX][0-9a-fA-F]{64}", value):
        raise ValueError("ONCHAIN_POOL_ID_TYPE_UNVERIFIED")
    pool_address = _address(value, network)
    return pool_address, pool_address, "ADDRESS", dex_id


def _pools(payload, receipt, network, address, missing):
    query = payload.get("query")
    if (
        not isinstance(query, dict)
        or _network(query.get("network")) != network
        or _address(query.get("token_address"), network) != address
        or query.get("limit") != 3
        or query.get("order_by") != "volume_usd_24h"
        or query.get("sort") != "desc"
    ):
        raise ValueError("ONCHAIN_POOL_QUERY_MISMATCH")
    rows, next_page = payload.get("results"), payload.get("has_next_page")
    if not isinstance(rows, list) or len(rows) > 3 or type(next_page) is not bool:
        raise ValueError("ONCHAIN_POOL_COVERAGE_UNKNOWN")
    selected, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or _network(row.get("chain")) != network:
            raise ValueError("ONCHAIN_POOL_IDENTITY_MISMATCH")
        pool_id, pool_address, identity_kind, dex_id = _pool_identity(row, network)
        if pool_id in seen:
            raise ValueError("ONCHAIN_POOL_DUPLICATE")
        seen.add(pool_id)
        tokens = row.get("tokens")
        if not isinstance(tokens, list) or not 1 <= len(tokens) <= 8:
            raise ValueError("ONCHAIN_POOL_TOKEN_IDENTITY_MISSING")
        target_found = False
        for token in tokens:
            if not isinstance(token, dict) or _network(token.get("chain")) != network:
                raise ValueError("ONCHAIN_POOL_TOKEN_IDENTITY_MISMATCH")
            target_found |= _address(token.get("id"), network) == address
        if not target_found:
            raise ValueError("ONCHAIN_POOL_TOKEN_IDENTITY_MISMATCH")
        item = {
            "pool_id": pool_id,
            "pool_address": pool_address,
            "identity_kind": identity_kind,
            "dex_id": dex_id,
            "event_time_ms": None,
            "freshness": "UNVERIFIED",
        }
        for field in ("volume_usd_24h", "liquidity_usd", "transactions_24h"):
            if field not in row:
                item[field] = None
                missing.add(f"pools.{pool_id}.{field}")
            else:
                item[field] = (
                    _integer(row[field])
                    if field == "transactions_24h"
                    else _text(_number(row[field]))
                )
        selected.append(item)
    return {
        "status": "PARTIAL",
        "coverage": "PARTIAL",
        "freshness": "UNVERIFIED",
        "received_at_ms": receipt,
        "event_time_ms": None,
        "selection": "TOP_3_REPORTED_24H_VOLUME_NOT_ALL_POOLS_OR_MARKETS",
        "has_next_page": next_page,
        "reported_pools": selected,
        "liquidity_change_interpretation": "NOT_LP_WITHDRAWAL_EVIDENCE",
        "historical_available": False,
    }


def describe(bundle, mapping, *, as_of_ms, max_age_ms=300_000):
    """Fail closed within research; missing/malformed evidence never authorizes orders."""
    raw = bundle if isinstance(bundle, dict) else {}

    def identity(name):
        value = raw.get(name)
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value)
            else None
        )

    result = {
        "version": VERSION,
        "source": SOURCE,
        "provider": PROVIDER,
        "symbol": identity("symbol"),
        "network_id": identity("network_id"),
        "token_address": identity("token_address"),
        "mapping_id": identity("mapping_id"),
        "status": "UNKNOWN",
        "historical_available": False,
        "metrics": {
            "provider_aggregate": _unknown("ONCHAIN_EVIDENCE_UNAVAILABLE"),
            "pools": _unknown("ONCHAIN_EVIDENCE_UNAVAILABLE"),
            **{
                name: _unknown("NOT_PROVIDED_BY_AGGREGATE_ENDPOINT")
                for name in (
                    "actual_swap_events",
                    "lp_changes",
                    "exchange_wallet_flows",
                    "block_identity",
                    "finality",
                    "reorg_proof",
                )
            },
        },
        "missing_fields": [],
        "reasons": [],
        "limitations": list(LIMITATIONS),
        **dict.fromkeys(AUTH_FIELDS, False),
    }
    missing, reasons = set(), set()
    try:
        network, address, start, finish = _scope(bundle, mapping, as_of_ms, max_age_ms)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, ValueError)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", str(exc))
            else "ONCHAIN_EVIDENCE_INVALID"
        )
        result["reasons"] = [reason]
        return result
    with localcontext() as ctx:
        ctx.prec = 50
        for name, target in (("token", "provider_aggregate"), ("pools", "pools")):
            try:
                payload, receipt = _component(
                    bundle, name, start, finish, as_of_ms, max_age_ms, network, address
                )
                if name == "token":
                    result["metrics"][target] = _aggregate(
                        payload,
                        mapping,
                        receipt,
                        as_of_ms,
                        max_age_ms,
                        network,
                        address,
                        missing,
                    )
                else:
                    result["metrics"][target] = _pools(
                        payload, receipt, network, address, missing
                    )
            except (
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
                InvalidOperation,
                RecursionError,
            ) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", str(exc))
                    else "ONCHAIN_EVIDENCE_INVALID"
                )
                result["metrics"][target] = _unknown(reason)
                reasons.add(f"{name.upper()}_{reason}")
                missing.add(name)
    observed = result["metrics"]["provider_aggregate"]["status"] != "UNKNOWN"
    pools_observed = result["metrics"]["pools"]["status"] != "UNKNOWN"
    # Pool freshness and aggregate-only coverage prevent a full on-chain claim.
    result["status"] = "PARTIAL" if observed or pools_observed else "UNKNOWN"
    result["missing_fields"] = sorted(missing)
    result["reasons"] = sorted(
        reasons
        | {"AGGREGATE_ONLY_NO_EVENT_FINALITY", "POOL_EVENT_FRESHNESS_UNVERIFIED"}
    )
    return result
