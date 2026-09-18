"""Canonical exchange-position slot identity with deterministic serialization."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

from position_identity.principal import AccountPrincipal


class Exchange(str, Enum):
    BINANCE = 'BINANCE'


class Product(str, Enum):
    FUTURES = 'FUTURES'


class SlotEnvironment(str, Enum):
    PROD = 'PROD'
    DEMO = 'DEMO'
    SANDBOX = 'SANDBOX'


class PositionMode(str, Enum):
    ONE_WAY = 'ONE_WAY'
    HEDGE = 'HEDGE'


class SlotSide(str, Enum):
    BOTH = 'BOTH'
    LONG = 'LONG'
    SHORT = 'SHORT'


_EnumT = TypeVar('_EnumT', bound=Enum)


def _normalize_enum(value, enum_type: type[_EnumT], aliases: dict[str, str],
                    field: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TypeError(f'{field} must be a string or {enum_type.__name__}')
    normalized = value.strip().upper().replace('-', '_').replace(' ', '_')
    normalized = aliases.get(normalized, normalized)
    try:
        return enum_type(normalized)
    except ValueError as exc:
        raise ValueError(f'unsupported {field}: {value!r}') from exc


def normalize_exchange(value) -> Exchange:
    return _normalize_enum(value, Exchange, {}, 'exchange')


def normalize_product(value) -> Product:
    aliases = {'USD_M_FUTURES': 'FUTURES', 'USD_M': 'FUTURES'}
    return _normalize_enum(value, Product, aliases, 'product')


def normalize_environment(value) -> SlotEnvironment:
    aliases = {
        'PRODUCTION': 'PROD',
        'MAINNET': 'PROD',
        'TEST': 'DEMO',
        'TESTNET': 'DEMO',
        'PAPER': 'SANDBOX',
    }
    return _normalize_enum(value, SlotEnvironment, aliases, 'environment')


def normalize_position_mode(value) -> PositionMode:
    aliases = {'ONEWAY': 'ONE_WAY', 'DUAL_SIDE': 'HEDGE'}
    return _normalize_enum(value, PositionMode, aliases, 'position mode')


def normalize_slot_side(value) -> SlotSide:
    return _normalize_enum(value, SlotSide, {}, 'slot side')


def slot_side_for_strategy(position_mode, strategy_side: str) -> SlotSide:
    """Map strategy LONG/SHORT to the physical exchange slot side."""
    mode = normalize_position_mode(position_mode)
    if not isinstance(strategy_side, str):
        raise TypeError('strategy side must be LONG or SHORT')
    side = strategy_side.strip().upper()
    if side not in ('LONG', 'SHORT'):
        raise ValueError('strategy side must be LONG or SHORT')
    if mode is PositionMode.ONE_WAY:
        return SlotSide.BOTH
    return SlotSide(side)


def _normalize_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError('symbol must be a string')
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError('symbol is required')
    if any(ord(char) <= 32 or ord(char) == 127 for char in symbol):
        raise ValueError('symbol contains whitespace or control characters')
    return symbol


@dataclass(frozen=True)
class ExchangePositionKey:
    """Immutable identity for one reusable physical exchange-position slot."""

    exchange: Exchange | str
    product: Product | str
    environment: SlotEnvironment | str
    account_principal_id: AccountPrincipal | str
    position_mode: PositionMode | str
    symbol: str
    slot_side: SlotSide | str

    STORAGE_KEY_PREFIX = 'pm:slot:v1'

    def __post_init__(self) -> None:
        principal = self.account_principal_id
        if not isinstance(principal, AccountPrincipal):
            principal = AccountPrincipal(principal)
        exchange = normalize_exchange(self.exchange)
        product = normalize_product(self.product)
        environment = normalize_environment(self.environment)
        mode = normalize_position_mode(self.position_mode)
        side = normalize_slot_side(self.slot_side)
        symbol = _normalize_symbol(self.symbol)

        if mode is PositionMode.ONE_WAY and side is not SlotSide.BOTH:
            raise ValueError('ONE_WAY position mode requires BOTH slot side')
        if mode is PositionMode.HEDGE and side is SlotSide.BOTH:
            raise ValueError('HEDGE position mode requires LONG or SHORT slot side')

        object.__setattr__(self, 'exchange', exchange)
        object.__setattr__(self, 'product', product)
        object.__setattr__(self, 'environment', environment)
        object.__setattr__(self, 'account_principal_id', principal)
        object.__setattr__(self, 'position_mode', mode)
        object.__setattr__(self, 'symbol', symbol)
        object.__setattr__(self, 'slot_side', side)

    @classmethod
    def one_way(cls, *, account_principal_id: AccountPrincipal | str,
                environment: SlotEnvironment | str, symbol: str,
                exchange: Exchange | str = Exchange.BINANCE,
                product: Product | str = Product.FUTURES):
        """Build the repository's current BINANCE FUTURES ONE_WAY/BOTH slot."""
        return cls(
            exchange=exchange,
            product=product,
            environment=environment,
            account_principal_id=account_principal_id,
            position_mode=PositionMode.ONE_WAY,
            symbol=symbol,
            slot_side=SlotSide.BOTH,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            'account_principal_id': self.account_principal_id.account_principal_id,
            'environment': self.environment.value,
            'exchange': self.exchange.value,
            'position_mode': self.position_mode.value,
            'product': self.product.value,
            'slot_side': self.slot_side.value,
            'symbol': self.symbol,
        }

    def to_canonical_string(self) -> str:
        """Return length-safe canonical JSON, never Python repr."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':'),
            ensure_ascii=True,
        )

    def canonical_digest(self) -> str:
        return hashlib.sha256(self.to_canonical_string().encode('ascii')).hexdigest()

    def to_storage_key(self) -> str:
        return f'{self.STORAGE_KEY_PREFIX}:{self.canonical_digest()}'

    def __hash__(self) -> int:
        digest = hashlib.sha256(
            self.to_canonical_string().encode('ascii')).digest()
        return int.from_bytes(digest[:8], byteorder='big', signed=True)
