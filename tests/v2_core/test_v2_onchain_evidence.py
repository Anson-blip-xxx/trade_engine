from copy import deepcopy
from decimal import localcontext

import pytest

from v2_core.evidence import canonical, digest
from v2_core.onchain_evidence import (
    AUTH_FIELDS,
    MAPPING_FIELDS,
    SOURCE,
    VERSION,
    describe,
)

NOW = 1_793_439_990_000  # 2026-10-31T09:46:30Z
ADDRESS = "0xc07e1300dc138601fa6b0b59f8d0fa477e690589"
POOL = "0x" + "1" * 40
OTHER = "0x" + "2" * 40


def seal_mapping(value):
    value["input_digest"] = digest(
        canonical({key: value[key] for key in MAPPING_FIELDS})
    )
    return value


def mapping():
    return seal_mapping(
        {
            "version": "onchain-asset-map-v1",
            "provider": "DEXPAPRIKA",
            "symbol": "QUSDT",
            "network_id": "bsc",
            "token_address": ADDRESS,
            "contract_multiplier": "1",
            "status": "VERIFIED",
            "verified_at_ms": NOW - 30_000,
            "valid_until_ms": NOW + 86_400_000,
            "source_links": [
                "https://quackai.ai/blog/introducing-q-powering-the-future-of-ai-governance"
            ],
            "reason_code": None,
            "mapping_id": "e33597db-6d4e-4fa9-b425-f592986284a4",
            "first_received_at_ms": NOW - 20_000,
        }
    )


def seal(value):
    value["sha256"] = digest(canonical({"payload": value["payload"]}))
    return value


def bundle():
    summary = {
        "chain": "bsc",
        "id": ADDRESS,
        "price_usd": "0.05",
        "liquidity_usd": "1000",
        "fdv": "400000000",
        "pools": 4,
    }
    for window in ("1m", "5m", "15m", "1h"):
        summary[window] = {
            "buy_usd": "30",
            "sell_usd": "10",
            "volume_usd": "40",
            "buys": 2,
            "sells": 1,
            "txns": 3,
        }
    token = {
        "id": ADDRESS,
        "chain": "bsc",
        "symbol": "Q",
        "no_index": False,
        "last_updated": "2026-10-31T09:46:30Z",
        "summary": summary,
    }
    pools = {
        "results": [
            {
                "id": POOL,
                "chain": "bsc",
                "tokens": [
                    {"id": ADDRESS, "chain": "bsc"},
                    {"id": OTHER, "chain": "bsc"},
                ],
                "volume_usd_24h": "100",
                "liquidity_usd": "50",
                "transactions_24h": 6,
                "price_usd": "999",
                "created_at": "2025-01-01T00:00:00Z",
            }
        ],
        "has_next_page": True,
        "query": {
            "network": "bsc",
            "token_address": ADDRESS,
            "limit": 3,
            "order_by": "volume_usd_24h",
            "sort": "desc",
        },
    }
    return {
        "version": VERSION,
        "source": SOURCE,
        "provider": "DEXPAPRIKA",
        "symbol": "QUSDT",
        "network_id": "bsc",
        "token_address": ADDRESS,
        "mapping_id": mapping()["mapping_id"],
        "started_at_ms": NOW - 10_000,
        "finished_at_ms": NOW,
        "components": {
            name: seal(
                {
                    "status": "OBSERVED",
                    "reason": None,
                    "retry_after_seconds": None,
                    "fetched_at_ms": NOW,
                    "endpoint": endpoint,
                    "payload": payload,
                }
            )
            for name, endpoint, payload in (
                ("token", f"/networks/bsc/tokens/{ADDRESS}", token),
                ("pools", "/networks/bsc/pools/search", pools),
            )
        },
        **dict.fromkeys(AUTH_FIELDS, False),
    }


def report(value=None, identity=None, **kwargs):
    return describe(
        bundle() if value is None else value,
        mapping() if identity is None else identity,
        as_of_ms=kwargs.pop("as_of_ms", NOW),
        **kwargs,
    )


def aggregate(value=None, **kwargs):
    return report(value, **kwargs)["metrics"]["provider_aggregate"]


def test_observed_aggregate_preserves_windows_without_trade_authority():
    result = report()
    assert result["status"] == "PARTIAL"
    assert all(result[key] is False for key in AUTH_FIELDS)
    assert result["historical_available"] is False
    metric = result["metrics"]["provider_aggregate"]
    assert metric["status"] == "OBSERVED_AGGREGATE"
    assert metric["provider_updated_at_ms"] == NOW
    assert metric["event_time_ms"] is None
    assert set(metric["windows"]) == {"1m", "5m", "15m", "1h"}
    assert metric["windows"]["1m"]["signed_amount_imbalance"] == "0.5"
    assert "total_flow" not in metric
    for name in (
        "actual_swap_events",
        "lp_changes",
        "exchange_wallet_flows",
        "block_identity",
        "finality",
        "reorg_proof",
    ):
        assert result["metrics"][name]["status"] == "UNKNOWN"


def test_pool_price_and_creation_time_never_become_token_price_or_freshness():
    result = report()["metrics"]["pools"]
    assert result["freshness"] == "UNVERIFIED"
    assert result["coverage"] == "PARTIAL"
    assert result["has_next_page"] is True
    row = result["reported_pools"][0]
    assert "price_usd" not in row
    assert "created_at" not in row
    assert row["event_time_ms"] is None
    assert result["liquidity_change_interpretation"] == "NOT_LP_WITHDRAWAL_EVIDENCE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "UNSUPPORTED"),
        ("status", "REVOKED"),
        ("provider", "OTHER"),
        ("verified_at_ms", NOW + 1),
        ("valid_until_ms", NOW),
        ("first_received_at_ms", NOW + 1),
        ("first_received_at_ms", NOW - 1),
        ("token_address", OTHER),
        ("network_id", "ethereum"),
        ("symbol", "AKEUSDT"),
        ("contract_multiplier", "0"),
        ("contract_multiplier", "NaN"),
        ("mapping_id", "not-a-uuid"),
    ],
)
def test_unverified_unavailable_or_wrong_identity_is_unknown(field, value):
    identity = mapping()
    identity[field] = value
    if field in MAPPING_FIELDS:
        seal_mapping(identity)
    result = report(identity=identity)
    assert result["status"] == "UNKNOWN"
    assert all(result[key] is False for key in AUTH_FIELDS)


def test_mapping_digest_mismatch_rejected():
    identity = mapping()
    identity["source_links"] = ["https://example.org/"]
    assert report(identity=identity)["reasons"] == ["ONCHAIN_MAPPING_DIGEST_MISMATCH"]


def test_no_mapping_is_explicit_unknown():
    assert describe(bundle(), None, as_of_ms=NOW)["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "UNKNOWN"),
        ("provider", "OTHER"),
        ("source_host", "wrong.example"),
        ("mapping_id", "ed675bbe-e2c5-45f4-8bbd-3c7fd28c2438"),
        ("execution_authorized", True),
        ("promotion_authorized", True),
        ("automatic_parameter_updates", True),
        ("finished_at_ms", NOW + 1),
    ],
)
def test_bad_bundle_scope_is_unknown(field, value):
    value_bundle = bundle()
    value_bundle[field] = value
    assert report(value_bundle)["status"] == "UNKNOWN"


def test_expired_receipt_is_not_reused():
    assert report(as_of_ms=NOW + 300_001)["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-31T09:46:31Z",
        "2026-10-31T09:40:00Z",
        "2026-10-31T09:46:30",
        "2026-10-31T09:46:30+00:00",
        "invalid",
        "2026-99-31T09:46:30Z",
        None,
    ],
)
def test_missing_stale_future_or_ambiguous_provider_time_is_unknown(timestamp):
    value = bundle()
    value["components"]["token"]["payload"]["last_updated"] = timestamp
    seal(value["components"]["token"])
    assert aggregate(value)["status"] == "UNKNOWN"


def test_fractional_utc_time_is_supported_without_fake_block_time():
    value = bundle()
    value["components"]["token"]["payload"]["last_updated"] = (
        "2026-10-31T09:46:29.123456Z"
    )
    seal(value["components"]["token"])
    assert aggregate(value)["provider_updated_at_ms"] == NOW - 877


@pytest.mark.parametrize(
    "path,invalid",
    [
        (("id",), OTHER),
        (("chain",), "ethereum"),
        (("symbol",), "AKE"),
        (("summary", "id"), OTHER),
        (("summary", "chain"), "ethereum"),
        (("no_index",), True),
        (("summary", "liquidity_usd"), "NaN"),
        (("summary", "price_usd"), "Infinity"),
        (("summary", "pools"), True),
    ],
)
def test_contradictory_identity_or_invalid_numeric_payload_is_unknown(path, invalid):
    value = bundle()
    target = value["components"]["token"]["payload"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid
    seal(value["components"]["token"])
    assert aggregate(value)["status"] == "UNKNOWN"


def test_evm_address_case_is_normalized_but_not_network_substitution():
    value = bundle()
    value["components"]["token"]["payload"]["id"] = ADDRESS.upper()
    value["components"]["token"]["payload"]["summary"]["id"] = ADDRESS.upper()
    seal(value["components"]["token"])
    assert aggregate(value)["status"] == "OBSERVED_AGGREGATE"


def test_counts_are_not_used_as_missing_amounts():
    value = bundle()
    raw = value["components"]["token"]["payload"]["summary"]["1m"]
    del raw["buy_usd"]
    raw["buys"], raw["sells"] = 999, 1
    seal(value["components"]["token"])
    metric = aggregate(value)
    assert metric["windows"]["1m"]["status"] == "UNKNOWN"
    assert metric["windows"]["5m"]["status"] == "OBSERVED_AGGREGATE"


def test_zero_amounts_are_real_zero_not_directional_signal():
    value = bundle()
    raw = value["components"]["token"]["payload"]["summary"]["1m"]
    raw["buy_usd"] = raw["sell_usd"] = "0"
    seal(value["components"]["token"])
    metric = aggregate(value)["windows"]["1m"]
    assert metric["buy_usd"] == metric["sell_usd"] == "0"
    assert metric["signed_amount_imbalance"] is None
    assert metric["imbalance_reason"] == "ZERO_REPORTED_AMOUNT"


@pytest.mark.parametrize("value", ["-1", "NaN", "Inf", "1e100", "1e-100", True, None])
def test_bad_window_amount_isolated_without_filling_zero(value):
    data = bundle()
    data["components"]["token"]["payload"]["summary"]["5m"]["buy_usd"] = value
    seal(data["components"]["token"])
    metric = aggregate(data)
    assert metric["windows"]["5m"]["status"] == "UNKNOWN"
    assert "buy_usd" not in metric["windows"]["5m"]
    assert metric["windows"]["1m"]["status"] == "OBSERVED_AGGREGATE"


def test_broken_component_does_not_hide_other_component():
    value = bundle()
    value["components"]["token"]["sha256"] = "0" * 64
    result = report(value)
    assert result["metrics"]["provider_aggregate"]["status"] == "UNKNOWN"
    assert result["metrics"]["pools"]["status"] == "PARTIAL"
    assert result["status"] == "PARTIAL"


def test_float_nan_fails_without_serializing_non_finite():
    value = bundle()
    value["components"]["token"]["payload"]["summary"]["price_usd"] = float("nan")
    result = report(value)
    assert result["metrics"]["provider_aggregate"]["status"] == "UNKNOWN"
    canonical(result)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_query",
        "wrong_token",
        "wrong_chain",
        "duplicate",
        "missing_next",
        "too_many",
        "invalid_amount",
    ],
)
def test_pool_identity_and_bounds(mutation):
    value = bundle()
    raw = value["components"]["pools"]["payload"]
    if mutation == "wrong_query":
        raw["query"]["token_address"] = OTHER
    elif mutation == "wrong_token":
        raw["results"][0]["tokens"] = [{"id": OTHER, "chain": "bsc"}]
    elif mutation == "wrong_chain":
        raw["results"][0]["chain"] = "ethereum"
    elif mutation == "duplicate":
        raw["results"].append(deepcopy(raw["results"][0]))
    elif mutation == "missing_next":
        del raw["has_next_page"]
    elif mutation == "too_many":
        raw["results"] *= 4
    else:
        raw["results"][0]["liquidity_usd"] = "NaN"
    seal(value["components"]["pools"])
    result = report(value)
    assert result["metrics"]["pools"]["status"] == "UNKNOWN"
    assert result["metrics"]["provider_aggregate"]["status"] == "OBSERVED_AGGREGATE"


def test_empty_or_last_page_does_not_claim_whole_market_coverage():
    value = bundle()
    value["components"]["pools"]["payload"].update(results=[], has_next_page=False)
    seal(value["components"]["pools"])
    metric = report(value)["metrics"]["pools"]
    assert metric["reported_pools"] == []
    assert metric["coverage"] == "PARTIAL"


def test_missing_fields_are_none_not_fabricated_zero():
    value = bundle()
    del value["components"]["token"]["payload"]["summary"]["liquidity_usd"]
    del value["components"]["token"]["payload"]["summary"]["1m"]["txns"]
    seal(value["components"]["token"])
    metric = aggregate(value)
    assert metric["liquidity_usd"] is None
    assert metric["windows"]["1m"]["txns"] is None
    assert "token.summary.liquidity_usd" in report(value)["missing_fields"]


def test_global_decimal_context_does_not_change_output():
    baseline = report()
    with localcontext() as ctx:
        ctx.prec = 2
        assert report() == baseline


def test_inputs_unchanged_and_result_json_is_safe():
    data, identity = bundle(), mapping()
    originals = deepcopy((data, identity))
    canonical(report(data, identity))
    assert (data, identity) == originals


def test_unknown_provider_error_preserved_as_safe_code():
    value = bundle()
    component = value["components"]["token"]
    component.update(
        status="UNKNOWN", payload=None, reason="HTTP_RATE_LIMITED", sha256=None
    )
    assert aggregate(value) == {"status": "UNKNOWN", "reason": "HTTP_RATE_LIMITED"}


def test_raw_error_text_never_leaks_into_report():
    value = bundle()
    value["components"]["token"].update(
        status="UNKNOWN", payload=None, reason="password=should_not_escape"
    )
    assert "should_not_escape" not in canonical(report(value))


@pytest.mark.parametrize("name", ["token", "pools"])
def test_future_component_receipt_is_unknown(name):
    value = bundle()
    value["components"][name]["fetched_at_ms"] = NOW + 1
    target = "provider_aggregate" if name == "token" else "pools"
    assert report(value)["metrics"][target]["status"] == "UNKNOWN"


def test_mapping_and_bundle_uuid_aliases_match_without_accepting_other_ids():
    value = bundle()
    value["mapping_id"] = value["mapping_id"].upper()
    assert aggregate(value)["status"] == "OBSERVED_AGGREGATE"


@pytest.mark.parametrize("part", ["components", "token", "pools"])
def test_absent_components_are_research_unknown(part):
    value = bundle()
    if part == "components":
        del value[part]
        assert report(value)["status"] == "UNKNOWN"
    else:
        del value["components"][part]
        target = "provider_aggregate" if part == "token" else "pools"
        assert report(value)["metrics"][target]["status"] == "UNKNOWN"


def test_unknown_report_does_not_echo_arbitrary_identity_payload():
    value = bundle()
    value["symbol"] = {"password": "must_not_echo"}
    result = report(value)
    assert result["symbol"] is None
    assert "must_not_echo" not in canonical(result)


def test_missing_summary_identity_is_not_inferred_from_outer_token():
    value = bundle()
    del value["components"]["token"]["payload"]["summary"]["id"]
    seal(value["components"]["token"])
    assert aggregate(value)["status"] == "UNKNOWN"


@pytest.mark.parametrize("max_age_ms", [0, True, -1, 3_600_001, "300000"])
def test_invalid_age_policy_does_not_produce_usable_evidence(max_age_ms):
    assert report(max_age_ms=max_age_ms)["status"] == "UNKNOWN"


def test_non_dict_input_returns_json_safe_unknown():
    result = describe([], None, as_of_ms=NOW)
    assert result["status"] == "UNKNOWN"
    canonical(result)


@pytest.mark.parametrize(
    "pool_id",
    [
        "0x478064492057be4bb5a34f6570215260f66997d5a939b2ec44583e5c4c29a618",
        "0x0587d2584ab25bdfb591bcf395519c1fd57967eac64c8c691268020752261f71",
    ],
)
def test_real_uniswap_v4_bytes32_ids_are_not_pool_addresses(pool_id):
    value = bundle()
    raw = value["components"]["pools"]["payload"]
    v4 = deepcopy(raw["results"][0])
    v4.update(id=pool_id, dex_id="uniswap_v4")
    raw["results"].append(v4)
    seal(value["components"]["pools"])
    metric = report(value)["metrics"]["pools"]
    assert metric["status"] == "PARTIAL"
    assert len(metric["reported_pools"]) == 2
    previous, new = metric["reported_pools"]
    assert previous["pool_id"] == previous["pool_address"] == POOL
    assert previous["identity_kind"] == "ADDRESS"
    assert new["pool_id"] == pool_id
    assert new["pool_address"] is None
    assert new["identity_kind"] == "BYTES32_POOL_ID"
    assert new["dex_id"] == "uniswap_v4"
    assert new["freshness"] == "UNVERIFIED"


@pytest.mark.parametrize(
    "pool_id",
    [
        POOL,
        "0x" + "a" * 63,
        "0x" + "a" * 65,
        "a" * 64,
        "0x" + "g" * 64,
        "0x" + "a" * 64 + "\n",
        None,
        True,
        32,
    ],
)
def test_v4_pool_id_requires_exact_bytes32_hex(pool_id):
    value = bundle()
    value["components"]["pools"]["payload"]["results"][0].update(
        id=pool_id, dex_id="uniswap_v4"
    )
    seal(value["components"]["pools"])
    metric = report(value)["metrics"]["pools"]
    assert metric == {"status": "UNKNOWN", "reason": "ONCHAIN_V4_POOL_ID_INVALID"}
    assert aggregate(value)["status"] == "OBSERVED_AGGREGATE"


@pytest.mark.parametrize(
    "dex_id", [None, "uniswap_v3", "unknown_dex", "pancakeswap_infinity"]
)
def test_bytes32_ids_for_unreviewed_dex_types_degrade_to_unknown(dex_id):
    value = bundle()
    value["components"]["pools"]["payload"]["results"][0].update(
        id="0x" + "a" * 64, dex_id=dex_id
    )
    seal(value["components"]["pools"])
    assert report(value)["metrics"]["pools"] == {
        "status": "UNKNOWN",
        "reason": "ONCHAIN_POOL_ID_TYPE_UNVERIFIED",
    }


@pytest.mark.parametrize("location", ["token", "summary", "pool_token", "mapping"])
def test_bytes32_exception_never_weakens_evm_token_address_validation(location):
    value, identity = bundle(), mapping()
    long_id = "0x" + "a" * 64
    if location == "token":
        value["components"]["token"]["payload"]["id"] = long_id
        seal(value["components"]["token"])
    elif location == "summary":
        value["components"]["token"]["payload"]["summary"]["id"] = long_id
        seal(value["components"]["token"])
    elif location == "pool_token":
        value["components"]["pools"]["payload"]["results"][0]["tokens"][0]["id"] = (
            long_id
        )
        seal(value["components"]["pools"])
    else:
        identity["token_address"] = value["token_address"] = long_id
        seal_mapping(identity)
    result = report(value, identity)
    if location == "mapping":
        assert result["status"] == "UNKNOWN"
    else:
        target = "pools" if location == "pool_token" else "provider_aggregate"
        assert result["metrics"][target]["status"] == "UNKNOWN"


def test_duplicate_v4_pool_id_is_detected_after_case_normalization():
    value = bundle()
    raw = value["components"]["pools"]["payload"]
    raw["results"][0].update(id="0x" + "ab" * 32, dex_id="uniswap_v4")
    duplicate = deepcopy(raw["results"][0])
    duplicate["id"] = duplicate["id"].upper()
    raw["results"].append(duplicate)
    seal(value["components"]["pools"])
    assert report(value)["metrics"]["pools"] == {
        "status": "UNKNOWN",
        "reason": "ONCHAIN_POOL_DUPLICATE",
    }


def test_unknown_dex_with_address_shape_is_only_a_provider_reported_address():
    value = bundle()
    value["components"]["pools"]["payload"]["results"][0]["dex_id"] = "unknown_dex"
    seal(value["components"]["pools"])
    row = report(value)["metrics"]["pools"]["reported_pools"][0]
    assert row["pool_id"] == row["pool_address"] == POOL
    assert row["freshness"] == "UNVERIFIED"
    assert report(value)["metrics"]["finality"]["status"] == "UNKNOWN"
