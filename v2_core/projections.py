"""Non-authoritative, replay-safe Redis and ClickHouse projections."""

from v2_core.evidence import canonical, digest

_REDIS_CAS = """
local version = redis.call('HGET', KEYS[1], 'version')
local wanted = ARGV[1]
if version then
    -- Compare decimal integer strings without Lua floating-point truncation.
    if #version > #wanted or (#version == #wanted and version > wanted) then
        return 1
    end
    if version == wanted then
        if redis.call('HGET', KEYS[1], 'digest') == ARGV[3] then return 1 end
        return 0
    end
end
redis.call('HSET', KEYS[1], 'version', wanted, 'payload', ARGV[2], 'digest', ARGV[3])
return 1
"""


class RedisProjection:
    def __init__(self, redis_client, namespace):
        if not isinstance(namespace, str) or not namespace.startswith("v2:"):
            raise ValueError("explicit v2 namespace required")
        self.client, self.namespace = redis_client, namespace

    def put(self, entity_id, version, payload):
        if type(version) is not int or version < 1:
            raise ValueError("positive integer version required")
        encoded = canonical(payload)
        return (
            self.client.eval(
                _REDIS_CAS,
                1,
                self.namespace + ":" + entity_id,
                str(version),
                encoded,
                digest(encoded),
            )
            == 1
        )


class ClickHouseEvents:
    """Inject clickhouse-connect compatible insert; consumers query the FINAL view."""

    def __init__(self, client):
        self.client = client

    def __call__(self, event_id, intent_id, kind, payload):
        encoded = canonical(payload)
        self.client.insert(
            "v2_trade_events",
            [[event_id, intent_id, kind, encoded, digest(encoded)]],
            column_names=["event_id", "intent_id", "kind", "payload", "payload_digest"],
        )
        return True


class RedisTraces:
    """A cache miss is rebuilt from PG, never interpreted as no active position."""

    def __init__(self, connection_factory, projection):
        from v2_core.service import TradingData

        self._connect = connection_factory
        self.service = TradingData(connection_factory)
        self.projection = projection

    def __call__(self, _event_id, intent_id, _kind, _payload):
        trace = self.service.trace(intent_id)
        if trace is None:
            raise ValueError("outbox references unknown intent")
        return self.projection.put(intent_id, trace["data_revision"], trace)

    def rebuild_batch(self, *, after=None, limit=100):
        """Replay authoritative snapshots, independent of old delivery receipts.

        UUID pagination bounds memory. Concurrent admissions are covered by the
        normal outbox consumer; run it alongside rebuild. Resume cursor is only
        an optimization, never a business checkpoint or execution permission.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT intent_id::text FROM v2_trade_intents
                WHERE (%s::uuid IS NULL OR intent_id > %s::uuid)
                ORDER BY intent_id LIMIT %s""",
                (after, after, limit),
            ).fetchall()
        for (intent_id,) in rows:
            if self(None, intent_id, None, None) is not True:
                raise RuntimeError("cache rebuild was not acknowledged")
        return {"count": len(rows), "after": rows[-1][0] if rows else after}
