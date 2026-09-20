"""Explicit account-bound application queries; not a replacement for DB roles/RLS."""

FIELDS = ("exchange", "account_id", "environment", "product")


class ScopeMismatch(ValueError):
    pass


def validate_scope(scope):
    from v2_core.account_risk import AccountScope

    if scope is not None and not isinstance(scope, AccountScope):
        raise TypeError("typed account scope or explicit global compatibility required")
    return scope


def predicate(scope):
    """Static `i` alias is owned by the caller; all identity values are parameters."""
    validate_scope(scope)
    if scope is None:
        return "TRUE", ()
    return "(i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)", tuple(
        getattr(scope, field) for field in FIELDS
    )


def require_scope(scope, value):
    if scope is not None and any(
        value.get(field) != getattr(scope, field) for field in FIELDS
    ):
        raise ScopeMismatch("ACCOUNT_SCOPE_MISMATCH")
