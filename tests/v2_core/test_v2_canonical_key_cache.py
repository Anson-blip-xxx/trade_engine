"""Byte/error equivalence against the original canonical algorithm, without I/O."""

import gc
import hashlib
import json
import random
import weakref
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal

import pytest

from v2_core import evidence

ORIGINAL_CREDENTIAL_FIELDS = {
    "secret",
    "password",
    "apikey",
    "apisecret",
    "privatekey",
    "authorization",
    "bottoken",
    "accesstoken",
    "refreshtoken",
}


def original_canonical(value):
    """Frozen pre-optimization implementation; deliberately no cache/helper."""

    def validate(item):
        if item is None or type(item) in (str, int, bool):
            return
        if isinstance(item, list):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            if any(
                "".join(c for c in key.lower() if c.isalnum())
                in ORIGINAL_CREDENTIAL_FIELDS
                for key in item
            ):
                raise ValueError("credential secrets must not enter business records")
            for child in item.values():
                validate(child)
            return
        raise ValueError("JSON must use decimal strings instead of floats")

    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def result_of(port, value):
    try:
        return "ok", port(value)
    except Exception as exc:  # noqa: BLE001 - compare exact legacy failure contracts
        return "error", type(exc), exc.args


def assert_equivalent(value):
    expected = result_of(original_canonical, value)
    actual = result_of(evidence.canonical, value)
    assert actual == expected
    if actual[0] == "ok":
        assert actual[1].encode() == expected[1].encode()
        assert (
            evidence.digest(actual[1])
            == hashlib.sha256(expected[1].encode()).hexdigest()
        )


@pytest.fixture(autouse=True)
def fresh_key_cache():
    evidence._credential_field_cached.cache_clear()
    yield
    evidence._credential_field_cached.cache_clear()


@pytest.mark.parametrize("field", sorted(ORIGINAL_CREDENTIAL_FIELDS))
@pytest.mark.parametrize("variant", ["plain", "upper", "punctuated", "unicode"])
def test_credentials_at_any_depth_remain_rejected(field, variant):
    if variant == "upper":
        field = field.upper()
    elif variant == "punctuated":
        field = " _-.\t".join(field)
    elif variant == "unicode":
        field = "🙂\u200b\u0301".join(field)
    value = {"outer": [{"coverage": [{field: "synthetic-nonsecret-value"}]}]}
    assert_equivalent(value)
    with pytest.raises(ValueError, match="credential secrets must not enter"):
        evidence.canonical(value)


@pytest.mark.parametrize(
    "field",
    [
        "",
        "仓位",
        "İ",
        "İD",
        "ſecret",
        "ＳＥＣＲＥＴ",
        "API_KEY",
        "APİ_KEY",
        "𝑠ecret",
        "😀key",
        "a\x00b",
        "\ud800",
        "A\u030a",
        "Å",
        "secret1",
        "not_a_secret",
        "api_key_suffix",
        "\u202eAPI\u202cKEY",
    ],
)
def test_unicode_normalization_is_exactly_the_original_algorithm(field):
    value = {field: {"普通字段": [None, True, False, 0, -1, "\udfff"]}}
    assert_equivalent(value)
    assert_equivalent(value)  # The warm path must have identical behavior.


@pytest.mark.parametrize(
    "bad_value",
    [
        0.0,
        -0.0,
        float("nan"),
        float("inf"),
        -float("inf"),
        Decimal("0.1"),
        (1, 2),
        {1, 2},
        b"bytes",
        {1: "non-string-key"},
        {"safe": "value", 2: "invalid-key"},
    ],
)
def test_mutated_values_still_validate_after_keys_are_cached(bad_value):
    value = {"outer": [{"price": "1.00", "nested": {"balance": "2"}}]}
    evidence.canonical(value)
    before = evidence._credential_field_cached.cache_info()
    value["outer"][0]["nested"]["balance"] = bad_value
    assert_equivalent(value)
    assert evidence._credential_field_cached.cache_info().hits > before.hits
    with pytest.raises(ValueError, match="JSON must use decimal strings"):
        evidence.canonical(value)


def test_new_nested_credential_cannot_hide_behind_a_cached_parent_key():
    value = {"outer": [{"details": {"price": "1"}}]}
    evidence.canonical(value)
    value["outer"][0]["details"]["api-key"] = "synthetic-only"
    assert_equivalent(value)
    with pytest.raises(ValueError, match="credential secrets"):
        evidence.canonical(value)


@pytest.mark.parametrize("length", [129, 4096, 100_000])
@pytest.mark.parametrize("credential", [False, True])
def test_long_keys_never_enter_cache_even_when_they_normalize_to_credentials(
    length, credential
):
    field = ("_" * (length - 6) + "secret") if credential else "k" * length
    before = evidence._credential_field_cached.cache_info()
    assert_equivalent({field: "1"})
    assert_equivalent({field: "1"})
    assert evidence._credential_field_cached.cache_info() == before


def test_cache_length_boundary_is_inclusive():
    evidence.canonical({"k" * 128: "1"})
    first = evidence._credential_field_cached.cache_info()
    assert first.currsize == first.misses == 1
    evidence.canonical({"k" * 128: "2"})
    assert evidence._credential_field_cached.cache_info().hits == 1


def test_str_subclass_lower_is_evaluated_every_time_and_never_cached():
    class DynamicKey(str):
        masked = False

        def lower(self):
            return "api_key" if self.masked else super().lower()

    field = DynamicKey("normal_field")
    value = {field: "synthetic-only"}
    before = evidence._credential_field_cached.cache_info()
    assert_equivalent(value)
    field.masked = True
    assert_equivalent(value)
    with pytest.raises(ValueError, match="credential secrets"):
        evidence.canonical(value)
    field.masked = False
    assert_equivalent(value)
    assert evidence._credential_field_cached.cache_info() == before


def test_str_subclass_values_still_rejected_after_warmup():
    class Text(str):
        pass

    evidence.canonical({"value": "plain"})
    assert_equivalent({"value": Text("subclass")})
    with pytest.raises(ValueError, match="JSON must use decimal strings"):
        evidence.canonical({"value": Text("subclass")})


@pytest.mark.parametrize("root", [None, [], 1, True, "string", 1.0, ()])
def test_root_object_contract_does_not_change(root):
    assert_equivalent(root)


def test_cache_bound_and_eviction_never_skip_validation():
    for index in range(2100):
        assert_equivalent({f"safe_field_{index}": "1"})
    info = evidence._credential_field_cached.cache_info()
    assert info.currsize == info.maxsize == 1024
    assert info.misses == 2100
    assert_equivalent({"safe_field_0": "2"})
    assert evidence._credential_field_cached.cache_info().misses == 2101
    for _ in range(2):
        assert_equivalent({"safe_field_0": {"API_KEY": "synthetic-only"}})
    assert evidence._credential_field_cached.cache_info().currsize == 1024


def test_cache_does_not_retain_input_containers_or_values():
    class Object(dict):
        pass

    class Array(list):
        pass

    nested = Object(price="1")
    array = Array([nested])
    root = Object(rows=array)
    refs = [weakref.ref(value) for value in (root, array, nested)]
    assert_equivalent(root)
    del root, array, nested
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert evidence._credential_field_cached.cache_info().currsize > 0


def random_value(rng, depth=0):
    scalars = [None, True, False, 0, -1, 10**60, "", "1.234", "\ud800", "\n中😀"]
    keys = ["rows", "price", "symbol", "observed_at", "İ", "仓位", "", "apikey"]
    if depth >= 5 or rng.randrange(3) == 0:
        if rng.randrange(10) == 0:
            return rng.choice([0.1, Decimal(2), (3,), b"invalid"])
        return rng.choice(scalars)
    if rng.randrange(2):
        return [random_value(rng, depth + 1) for _ in range(rng.randrange(5))]
    return {
        rng.choice(keys) + rng.choice(["", "", "", "_safe"]): random_value(
            rng, depth + 1
        )
        for _ in range(rng.randrange(5))
    }


@pytest.mark.parametrize("seed", range(20))
def test_random_nested_documents_match_original_bytes_and_errors(seed):
    rng = random.Random(seed)
    for _ in range(100):
        value = {"root": random_value(rng)}
        assert_equivalent(value)
        assert_equivalent(deepcopy(value))


def test_concurrent_calls_match_original_with_bounded_shared_key_cache():
    def run(index):
        rng = random.Random(index)
        for number in range(40):
            value = {f"worker_{index}_{number}": random_value(rng)}
            assert_equivalent(value)
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(run, range(32)))
    assert evidence._credential_field_cached.cache_info().currsize <= 1024


def test_sorting_escaping_and_digest_golden_bytes():
    value = {"z": [True, None, -2], "a": '中\n"\\', "number": "1.00"}
    expected = '{"a":"\\u4e2d\\n\\"\\\\","number":"1.00","z":[true,null,-2]}'
    assert original_canonical(value) == expected
    for _ in range(3):
        assert evidence.canonical(value) == expected
        assert (
            evidence.digest(evidence.canonical(value))
            == hashlib.sha256(expected.encode()).hexdigest()
        )
