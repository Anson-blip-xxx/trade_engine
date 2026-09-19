"""Position protection services and dormant reliability primitives."""

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
from position_protection.writeback import WritebackCode, WritebackResult
from position_protection.writeback_redis import (
    RedisConditionalAliasWritebackAdapter,
)

__all__ = [
    'DESIRED_PROTECTION_SCHEMA_VERSION',
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
    'ProtectionVerificationResult',
    'RedisConditionalAliasWritebackAdapter',
    'RedisDesiredProtectionAdapter',
    'RedisNativeOpenHandoffAdapter',
    'RedisVerifiedActiveCommitAdapter',
    'VerifiedActiveCommitCode',
    'VerifiedActiveCommitResult',
    'WritebackCode',
    'WritebackResult',
    'verify_current_protection',
]
