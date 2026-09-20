"""Durable intent admission; acceptance does not authorize exchange execution."""
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID


@dataclass(frozen=True)
class OpenIntent:
    intent_id: str
    exchange: str
    account_id: str
    environment: str
    product: str
    producer: str
    request_key: str
    symbol: str
    side: str
    quantity: str
    strategy_version: str
    config_digest: str
    evidence_ref: str

    def __post_init__(self):
        object.__setattr__(self, 'intent_id', str(UUID(self.intent_id)))
        for name in ('exchange', 'account_id', 'environment', 'product',
                     'producer', 'request_key', 'symbol', 'strategy_version',
                     'evidence_ref'):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value or value != value.strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError('identity must be normalized nonempty text')
        if self.side not in {'BUY', 'SELL'}:
            raise ValueError('invalid side')
        if (not isinstance(self.config_digest, str) or
                len(self.config_digest) != 64 or
                any(c not in '0123456789abcdef' for c in self.config_digest)):
            raise ValueError('config_digest must be lowercase SHA-256')
        if not isinstance(self.quantity, str) or len(self.quantity) > 100:
            raise ValueError('quantity must be a bounded decimal string')
        try:
            number = Decimal(self.quantity)
        except InvalidOperation as exc:
            raise ValueError('invalid quantity') from exc
        if not number.is_finite() or number <= 0:
            raise ValueError('quantity must be finite and positive')
        if number >= Decimal('1e20') or number.as_tuple().exponent < -18:
            raise ValueError('quantity exceeds NUMERIC(38,18) precision')
        normalized = format(number, 'f')
        if '.' in normalized:
            normalized = normalized.rstrip('0').rstrip('.')
        object.__setattr__(self, 'quantity', normalized)

    @property
    def payload(self):
        return {name: getattr(self, name) for name in (
            'symbol', 'side', 'quantity', 'strategy_version',
            'config_digest', 'evidence_ref')}


class AdmissionCode(str, Enum):
    ACCEPTED = 'ACCEPTED'
    ALREADY_ACCEPTED = 'ALREADY_ACCEPTED'
    CONFLICT = 'CONFLICT'
    UNKNOWN = 'UNKNOWN'


@dataclass(frozen=True)
class Admission:
    code: AdmissionCode
    intent_id: str | None = None


_INSERT = """
INSERT INTO v2_trade_intents (
    intent_id, exchange, account_id, environment, product, producer,
    request_key, payload, payload_digest
) VALUES (
    %(intent_id)s, %(exchange)s, %(account_id)s, %(environment)s,
    %(product)s, %(producer)s, %(request_key)s, %(payload)s::jsonb,
    %(payload_digest)s
) ON CONFLICT DO NOTHING RETURNING intent_id::text
"""
_EXISTING = """
SELECT intent_id::text, payload, payload_digest FROM v2_trade_intents
WHERE exchange = %(exchange)s AND account_id = %(account_id)s
  AND environment = %(environment)s AND product = %(product)s
  AND producer = %(producer)s AND request_key = %(request_key)s
"""
_OUTBOX = """
INSERT INTO v2_domain_outbox (event_id, intent_id, event_type, payload)
VALUES (%(intent_id)s, %(intent_id)s, 'INTENT_ACCEPTED', %(payload)s::jsonb)
"""


class IntentStore:
    """Inject a transaction-scoped connection factory with tuple row results.

    Retry UNKNOWN with the same request key and content to resolve an ambiguous
    commit. Never turn a missing acknowledgement into a new trading request.
    """

    def __init__(self, connection_factory):
        if not callable(connection_factory):
            raise TypeError('connection_factory must be callable')
        self._connect = connection_factory

    def admit(self, intent):
        if not isinstance(intent, OpenIntent):
            raise TypeError('intent must be OpenIntent')
        payload = json.dumps(intent.payload, sort_keys=True, separators=(',', ':'))
        params = {name: getattr(intent, name) for name in (
            'intent_id', 'exchange', 'account_id', 'environment', 'product',
            'producer', 'request_key')}
        params.update(payload=payload,
                      payload_digest=hashlib.sha256(payload.encode()).hexdigest())
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_INSERT, params)
                inserted = cur.fetchone()
                if inserted:
                    cur.execute(_OUTBOX, params)
                    result = Admission(AdmissionCode.ACCEPTED, inserted[0])
                else:
                    cur.execute(_EXISTING, params)
                    existing = cur.fetchone()
                    if existing is None:
                        result = Admission(AdmissionCode.CONFLICT)
                    else:
                        stored = existing[1]
                        if isinstance(stored, str):
                            stored = json.loads(stored)
                        matches = (stored == intent.payload and
                                   existing[2] == params['payload_digest'])
                        result = Admission(
                            AdmissionCode.ALREADY_ACCEPTED if matches else
                            AdmissionCode.CONFLICT, existing[0])
            return result
        except Exception:  # noqa: BLE001 - driver/commit failure is ambiguous
            return Admission(AdmissionCode.UNKNOWN)
