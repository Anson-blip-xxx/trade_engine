"""Pure position-slot identity primitives.

This package is intentionally dormant: it performs no IO and is not wired into
the active trading runtime.
"""

from position_identity.adoption import (
    AdoptionClassification,
    AdoptionPlan,
    AuthorityExpectation,
    ClassificationDecision,
    OnboardingResult,
    OnboardingResultCode,
    QuarantineReason,
    ReconstructionPlan,
    apply_legacy_adoption,
    apply_reconstructed_episode,
    can_mutate_async,
    classify_legacy_active,
    classify_reconstructed_exposure,
    prepare_legacy_adoption,
    prepare_reconstructed_episode,
)
from position_identity.authority import (
    AUTHORITY_SCHEMA_VERSION,
    AuthorityAck,
    AuthorityAckCode,
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.authority_redis import RedisSlotAuthorityAdapter
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
    'AUTHORITY_SCHEMA_VERSION',
    'AccountPrincipal',
    'AdoptionClassification',
    'AdoptionPlan',
    'AuthorityAck',
    'AuthorityAckCode',
    'AuthorityExpectation',
    'AuthorityProvenance',
    'AuthorityReadCode',
    'AuthorityReadResult',
    'AuthorityStatus',
    'ClassificationDecision',
    'Exchange',
    'ExchangePositionKey',
    'OnboardingResult',
    'OnboardingResultCode',
    'PositionMode',
    'Product',
    'QuarantineReason',
    'ReconstructionPlan',
    'RedisSlotAuthorityAdapter',
    'SlotAuthority',
    'SlotEnvironment',
    'SlotSide',
    'apply_legacy_adoption',
    'apply_reconstructed_episode',
    'can_mutate_async',
    'classify_legacy_active',
    'classify_reconstructed_exposure',
    'prepare_legacy_adoption',
    'prepare_reconstructed_episode',
    'resolve_account_principal',
    'resolve_account_principal_from_config',
    'slot_side_for_strategy',
]
