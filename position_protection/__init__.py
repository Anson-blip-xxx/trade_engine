"""Position protection services and dormant reliability primitives."""

from position_protection.binance_observation import (
    BinanceObservationCode,
    BinanceObservationResult,
    normalize_binance_verification_snapshot,
)
from position_protection.desired import (
    DESIRED_PROTECTION_SCHEMA_VERSION,
    DesiredProtectionRecord,
    DesiredReadCode,
    DesiredReadResult,
    DesiredWriteCode,
    DesiredWriteResult,
    ProtectionStatus,
)
from position_protection.desired_redis import RedisDesiredProtectionAdapter
from position_protection.handoff import (
    NativeOpenHandoffCode,
    NativeOpenHandoffResult,
)
from position_protection.handoff_redis import RedisNativeOpenHandoffAdapter
from position_protection.task import ConditionalWritebackProtectionTask
from position_protection.verification import (
    ExchangeExposureObservation,
    ExchangeProtectionObservation,
    ProtectionVerificationCode,
    ProtectionVerificationResult,
    verify_current_protection,
)
from position_protection.verification_commit import (
    RedisVerifiedActiveCommitAdapter,
    VerifiedActiveCommitCode,
    VerifiedActiveCommitResult,
)
from position_protection.verification_coordinator import (
    BinanceVerificationSnapshot,
    ProtectionVerificationCoordinator,
    VerificationCoordinatorCode,
    VerificationCoordinatorResult,
    VerificationSession,
)
from position_protection.verification_retry import (
    VerificationRetryAction,
    VerificationRetryDecision,
    VerificationRetryPolicy,
    decide_verification_retry,
)
from position_protection.writeback import WritebackCode, WritebackResult
from position_protection.writeback_redis import (
    RedisConditionalAliasWritebackAdapter,
)

__all__ = [
    'DESIRED_PROTECTION_SCHEMA_VERSION',
    'BinanceObservationCode',
    'BinanceObservationResult',
    'BinanceVerificationSnapshot',
    'ConditionalWritebackProtectionTask',
    'DesiredProtectionRecord',
    'DesiredReadCode',
    'DesiredReadResult',
    'DesiredWriteCode',
    'DesiredWriteResult',
    'ExchangeExposureObservation',
    'ExchangeProtectionObservation',
    'NativeOpenHandoffCode',
    'NativeOpenHandoffResult',
    'ProtectionStatus',
    'ProtectionVerificationCode',
    'ProtectionVerificationCoordinator',
    'ProtectionVerificationResult',
    'RedisConditionalAliasWritebackAdapter',
    'RedisDesiredProtectionAdapter',
    'RedisNativeOpenHandoffAdapter',
    'RedisVerifiedActiveCommitAdapter',
    'VerificationCoordinatorCode',
    'VerificationCoordinatorResult',
    'VerificationRetryAction',
    'VerificationRetryDecision',
    'VerificationRetryPolicy',
    'VerificationSession',
    'VerifiedActiveCommitCode',
    'VerifiedActiveCommitResult',
    'WritebackCode',
    'WritebackResult',
    'decide_verification_retry',
    'normalize_binance_verification_snapshot',
    'verify_current_protection',
]
