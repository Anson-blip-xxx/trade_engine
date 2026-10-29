"""Synchronously release dedicated session locks before handing work onward.

Closing a client socket is not an acknowledgement that its PostgreSQL backend
has released session advisory locks. Keep the lock-only connection separate
from business transactions, and wait for the explicit unlock response.
"""

from contextlib import contextmanager


@contextmanager
def session_advisory_lock(connect, key):
    if not callable(connect) or not isinstance(key, str) or not 1 <= len(key) <= 8192:
        raise ValueError("EXPLICIT_SESSION_LOCK_REQUIRED")
    with connect() as owner:
        acquired, body_failed = False, False
        try:
            owner.execute("SET LOCAL statement_timeout='1000ms'")
            acquired = owner.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
            ).fetchone()[0]
            if type(acquired) is not bool:
                raise ValueError("INVALID_SESSION_LOCK_RESULT")
            owner.commit()
            yield acquired
        except BaseException:
            body_failed = True
            raise
        finally:
            if acquired:
                try:
                    # This connection owns no business work. Rollback only
                    # clears a failed/idle transaction before releasing its
                    # session lock, which survives commit and rollback alike.
                    owner.rollback()
                    owner.execute("SET LOCAL statement_timeout='1000ms'")
                    released = owner.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,)
                    ).fetchone()[0]
                    owner.commit()
                    if released is not True:
                        raise RuntimeError("SESSION_LOCK_RELEASE_UNCONFIRMED")
                except Exception:  # noqa: BLE001 - preserve body failure, never expose driver text
                    # Force socket closure as a fallback, but do not claim a
                    # clean handoff when its server acknowledgement was lost.
                    try:
                        owner.close()
                    except Exception:  # noqa: BLE001, S110 - outer context owns cleanup; never expose driver text
                        pass
                    if not body_failed:
                        raise RuntimeError("SESSION_LOCK_RELEASE_UNCONFIRMED") from None
