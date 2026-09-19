"""Position-slot identity primitives and strict Redis boundary adapters.

Runtime wiring is limited to explicitly configured V2 native open and canonical
close paths; importing this package performs no IO.
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
from position_identity.close_finalizer import (
    CanonicalCloseFence,
    CanonicalCloseFinalizer,
    CloseCaptureCode,
    CloseCaptureResult,
    CloseFinalizeCode,
    CloseFinalizeResult,
)
from position_identity.migration_handoff import (
    LegacyMigrationHandoffCode,
    LegacyMigrationHandoffResult,
    RedisLegacyMigrationHandoffAdapter,
)
from position_identity.principal import (
    ACCOUNT_PRINCIPAL_CONFIG_KEY,
    AccountPrincipal,
    resolve_account_principal,
    resolve_account_principal_from_config,
)
from position_identity.projection import (
    PROJECTION_SCHEMA_VERSION,
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionReadResult,
    ProjectionWriteCode,
    ProjectionWriteResult,
)
from position_identity.projection_migration import (
    LegacyProjectionCode,
    LegacyProjectionReason,
    LegacyProjectionResult,
    LegacyWriterDecision,
    legacy_snapshot_write_decision,
    prepare_legacy_projection,
)
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
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
    'PROJECTION_SCHEMA_VERSION',
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
    'CanonicalCloseFence',
    'CanonicalCloseFinalizer',
    'ClassificationDecision',
    'CloseCaptureCode',
    'CloseCaptureResult',
    'CloseFinalizeCode',
    'CloseFinalizeResult',
    'Exchange',
    'ExchangePositionKey',
    'LegacyMigrationHandoffCode',
    'LegacyMigrationHandoffResult',
    'LegacyProjectionCode',
    'LegacyProjectionReason',
    'LegacyProjectionResult',
    'LegacyWriterDecision',
    'LivePositionProjection',
    'OnboardingResult',
    'OnboardingResultCode',
    'PositionMode',
    'Product',
    'ProjectionReadCode',
    'ProjectionReadResult',
    'ProjectionWriteCode',
    'ProjectionWriteResult',
    'QuarantineReason',
    'ReconstructionPlan',
    'RedisLegacyMigrationHandoffAdapter',
    'RedisLivePositionProjectionAdapter',
    'RedisSlotAuthorityAdapter',
    'SlotAuthority',
    'SlotEnvironment',
    'SlotSide',
    'apply_legacy_adoption',
    'apply_reconstructed_episode',
    'can_mutate_async',
    'classify_legacy_active',
    'classify_reconstructed_exposure',
    'legacy_snapshot_write_decision',
    'prepare_legacy_adoption',
    'prepare_legacy_projection',
    'prepare_reconstructed_episode',
    'resolve_account_principal',
    'resolve_account_principal_from_config',
    'slot_side_for_strategy',
]
