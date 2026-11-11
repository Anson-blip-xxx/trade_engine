"""Bounded venue symbol syntax shared by research and execution boundaries.

Symbols retain the exchange's exact spelling: never case-fold, transliterate or
Unicode-normalize an identity. Syntax acceptance does not establish listing or
tradability; the execution environment's current contract catalog remains a gate.
"""

import re
from hashlib import sha256

# CJK Unified Ideographs and extensions A-I. Compatibility ideographs, fullwidth
# Latin, combining marks, whitespace and controls are deliberately not accepted.
_BASE = re.compile(
    r"[A-Z0-9\u3400-\u4dbf\u4e00-\u9fff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002b73f"
    r"\U0002b740-\U0002b81f\U0002b820-\U0002ceaf"
    r"\U0002ceb0-\U0002ebef\U0002ebf0-\U0002ee5f"
    r"\U00030000-\U0003134f\U00031350-\U000323af]{1,32}"
)


def contract_symbol(value, *, quote_assets=("USDT", "USDC")):
    """Return an exact USDT/USDC symbol, or raise the stable syntax error.

    USD is available only to callers explicitly displaying legacy USD contracts;
    the default trading contract remains USDT/USDC.
    """
    if (
        not isinstance(value, str)
        or not isinstance(quote_assets, tuple)
        or not quote_assets
        or any(quote not in {"USDT", "USDC", "USD"} for quote in quote_assets)
    ):
        raise ValueError("INVALID_SYMBOL")
    for quote in quote_assets:
        if value.endswith(quote) and _BASE.fullmatch(value[: -len(quote)]):
            return value
    raise ValueError("INVALID_SYMBOL")


def is_supported_symbol(value, *, quote_assets=("USDT", "USDC")):
    """Syntax support only; never substitute this for a venue catalog check."""
    try:
        contract_symbol(value, quote_assets=quote_assets)
    except ValueError:
        return False
    return True


def shared_frame_id(version, value, observed_at):
    """ASCII S3 identity, preserving every existing ASCII contract frame ID.

    Unicode symbols use a disjoint, bounded SHA-256 component. The original
    symbol remains in the frame and its digest; consumers reconstruct this ID
    from that exact symbol, version and observation clock.
    """
    contract_symbol(value)
    if (
        not isinstance(version, str)
        or re.fullmatch(r"s3-shared-v[1-9][0-9]{0,2}", version) is None
        or type(observed_at) is not int
        or not 0 <= observed_at <= 2**53 - 1
    ):
        raise ValueError("INVALID_FRAME_ID")
    component = (
        value if value.isascii() else "u-" + sha256(value.encode("utf-8")).hexdigest()
    )
    return f"{version}:{component}:{observed_at}"
