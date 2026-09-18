"""P10-07B unit tests for dormant canonical slot identity primitives."""
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from position_identity import (
    AccountPrincipal,
    ExchangePositionKey,
    PositionMode,
    SlotEnvironment,
    SlotSide,
    resolve_account_principal,
    resolve_account_principal_from_config,
    slot_side_for_strategy,
)

ROOT = Path(__file__).resolve().parents[2]


def _slot(*, principal='binance-main', environment='PROD', symbol='BTCUSDT'):
    return ExchangePositionKey.one_way(
        account_principal_id=principal,
        environment=environment,
        symbol=symbol,
    )


def test_explicit_principal_alias_is_trimmed_and_immutable():
    principal = resolve_account_principal('  binance-main  ')
    assert principal.account_principal_id == 'binance-main'
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.account_principal_id = 'other'


@pytest.mark.parametrize('value', [None, '', '   ', 'bad\nvalue', 7])
def test_missing_or_invalid_principal_fails_explicitly(value):
    with pytest.raises((TypeError, ValueError)):
        resolve_account_principal(value)


def test_principal_resolver_reads_only_explicit_config_field():
    principal = resolve_account_principal_from_config({
        'ACCOUNT_PRINCIPAL_ID': 'binance-sub1',
        'unrelated': 'ignored',
    })
    assert principal == AccountPrincipal('binance-sub1')
    with pytest.raises(ValueError):
        resolve_account_principal_from_config({})


def test_same_inputs_have_deterministic_serialization_and_hash():
    first = _slot()
    second = _slot()
    assert first == second
    assert hash(first) == hash(second)
    assert first.to_canonical_string() == second.to_canonical_string()
    assert first.to_storage_key() == second.to_storage_key()


def test_canonical_string_is_structured_json_not_repr():
    key = _slot()
    decoded = json.loads(key.to_canonical_string())
    assert decoded == key.to_dict()
    assert decoded['exchange'] == 'BINANCE'
    assert decoded['product'] == 'FUTURES'
    assert decoded['position_mode'] == 'ONE_WAY'
    assert decoded['slot_side'] == 'BOTH'


def test_prod_demo_and_sandbox_are_distinct():
    keys = {_slot(environment=value).to_storage_key()
            for value in ('PROD', 'demo', 'sandbox')}
    assert len(keys) == 3
    assert _slot(environment='production').environment is SlotEnvironment.PROD
    assert _slot(environment='testnet').environment is SlotEnvironment.DEMO


def test_logical_accounts_are_distinct_for_same_symbol():
    assert _slot(principal='binance-main') != _slot(principal='binance-sub1')
    assert _slot(principal='binance-main').to_storage_key() != \
        _slot(principal='binance-sub1').to_storage_key()


def test_long_and_short_strategies_share_one_way_both_slot():
    long_side = slot_side_for_strategy(PositionMode.ONE_WAY, 'LONG')
    short_side = slot_side_for_strategy('one-way', 'SHORT')
    assert long_side is short_side is SlotSide.BOTH
    assert ExchangePositionKey(
        'BINANCE', 'FUTURES', 'PROD', 'binance-main', 'ONE_WAY',
        'BTCUSDT', long_side,
    ) == ExchangePositionKey(
        'BINANCE', 'FUTURES', 'PROD', 'binance-main', 'ONE_WAY',
        'BTCUSDT', short_side,
    )


def test_system_position_and_request_ids_are_not_slot_inputs():
    s6 = _slot()
    s8 = _slot()
    legacy_a = _slot()
    legacy_b = _slot()
    request_a = _slot()
    request_b = _slot()
    assert s6 == s8
    assert legacy_a == legacy_b
    assert request_a == request_b
    fields = {field.name for field in dataclasses.fields(ExchangePositionKey)}
    assert fields.isdisjoint({'system', 'position_id', 'request_id'})


def test_symbol_is_trimmed_and_uppercased():
    assert _slot(symbol='  btcusdt ').symbol == 'BTCUSDT'
    assert _slot(symbol='  btcusdt ').to_storage_key() == \
        _slot(symbol='BTCUSDT').to_storage_key()


@pytest.mark.parametrize('symbol', ['', '   ', 'BTC USDT', 'BTC\nUSDT', None])
def test_invalid_symbol_rejected(symbol):
    with pytest.raises((TypeError, ValueError)):
        _slot(symbol=symbol)


def test_invalid_environment_mode_and_side_combinations_rejected():
    with pytest.raises(ValueError):
        _slot(environment='staging')
    with pytest.raises(ValueError):
        ExchangePositionKey(
            'BINANCE', 'FUTURES', 'PROD', 'binance-main', 'invalid',
            'BTCUSDT', 'BOTH',
        )
    with pytest.raises(ValueError):
        ExchangePositionKey(
            'BINANCE', 'FUTURES', 'PROD', 'binance-main', 'ONE_WAY',
            'BTCUSDT', 'LONG',
        )
    with pytest.raises(ValueError):
        ExchangePositionKey(
            'BINANCE', 'FUTURES', 'PROD', 'binance-main', 'HEDGE',
            'BTCUSDT', 'BOTH',
        )


def test_value_object_is_immutable_and_hashable():
    key = _slot()
    assert {key: 'value'}[key] == 'value'
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.symbol = 'ETHUSDT'


def test_special_principal_characters_cannot_collide_in_storage_key():
    first = _slot(principal='team|alpha:sub/account one')
    second = _slot(principal='team|alpha:sub/account two')
    assert first.to_storage_key() != second.to_storage_key()
    assert first.to_storage_key().startswith('pm:slot:v1:')
    assert 'team|alpha' not in first.to_storage_key()


def test_credential_rotation_data_is_not_an_identity_input():
    fields = {field.name for field in dataclasses.fields(ExchangePositionKey)}
    assert fields.isdisjoint({
        'api_key', 'api_secret', 'secret', 'private_key',
        'credential_fingerprint',
    })
    assert _slot() == _slot()


def test_hash_and_serialization_are_stable_in_clean_subprocess():
    code = (
        'from position_identity import ExchangePositionKey; '
        'k=ExchangePositionKey.one_way('
        'account_principal_id="binance-main", environment="PROD", '
        'symbol="BTCUSDT"); '
        'print(k.to_canonical_string()); print(k.to_storage_key()); print(hash(k))'
    )
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    first = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    second = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == ''


def test_clean_subprocess_import_has_no_runtime_modules_or_io_clients():
    code = (
        'import json, sys; import position_identity; '
        'blocked=("shared.position_manager", "redis", "requests", "psycopg", '
        '"position_lifecycle.service", "position_protection.service"); '
        'print(json.dumps([name for name in blocked if name in sys.modules]))'
    )
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == []
    assert result.stderr == ''
