"""Explicit, non-secret logical account identity."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

ACCOUNT_PRINCIPAL_CONFIG_KEY = 'ACCOUNT_PRINCIPAL_ID'


@dataclass(frozen=True)
class AccountPrincipal:
    """Stable logical exchange-account alias supplied by configuration."""

    account_principal_id: str

    def __post_init__(self) -> None:
        value = self.account_principal_id
        if not isinstance(value, str):
            raise TypeError('account principal must be a string')
        value = value.strip()
        if not value:
            raise ValueError('account principal is required')
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('account principal contains control characters')
        object.__setattr__(self, 'account_principal_id', value)


def resolve_account_principal(value: str | None) -> AccountPrincipal:
    """Resolve one explicit logical alias; missing input fails explicitly."""
    if value is None:
        raise ValueError('account principal is required')
    return AccountPrincipal(value)


def resolve_account_principal_from_config(
        config: Mapping[str, object]) -> AccountPrincipal:
    """Resolve the explicit principal field from an already-loaded mapping."""
    if not isinstance(config, Mapping):
        raise TypeError('principal config must be a mapping')
    value = config.get(ACCOUNT_PRINCIPAL_CONFIG_KEY)
    if value is not None and not isinstance(value, str):
        raise TypeError('account principal must be a string')
    return resolve_account_principal(value)
