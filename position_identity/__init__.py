"""Pure position-slot identity primitives.

This package is intentionally dormant: it performs no IO and is not wired into
the active trading runtime.
"""

from position_identity.principal import (
    ACCOUNT_PRINCIPAL_CONFIG_KEY,
    AccountPrincipal,
    resolve_account_principal,
    resolve_account_principal_from_config,
)
from position_identity.slot import (
    Exchange,
    ExchangePositionKey,
    PositionMode,
    Product,
    SlotEnvironment,
    SlotSide,
    slot_side_for_strategy,
)

__all__ = [
    'ACCOUNT_PRINCIPAL_CONFIG_KEY',
    'AccountPrincipal',
    'Exchange',
    'ExchangePositionKey',
    'PositionMode',
    'Product',
    'SlotEnvironment',
    'SlotSide',
    'resolve_account_principal',
    'resolve_account_principal_from_config',
    'slot_side_for_strategy',
]
