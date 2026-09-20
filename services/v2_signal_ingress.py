"""Explicit WSGI application factory for the independent V2 signal endpoint.

Importing this module performs no I/O and starts no service. No legacy imports,
Redis dedup, credential files, default schema, or trading capability.
"""

import os
import time

from v2_core.database import connection_factory
from v2_core.ingress import SignalIngress
from v2_core.signals import Signals
from v2_core.webhook import TradingViewWebhook


def create_application(*, environ=None, clock_ms=None):
    config = os.environ if environ is None else environ
    if config.get("V2_SIGNAL_INGRESS_ENABLED") != "YES":
        raise ValueError("V2 signal ingress is disabled")
    required = (
        "V2_POSTGRES_DSN",
        "V2_POSTGRES_SCHEMA",
        "V2_ENVIRONMENT",
        "V2_TV_WEBHOOK_SECRET",
        "V2_SIGNAL_MAX_AGE_MS",
        "V2_SIGNAL_MAX_LIFETIME_MS",
    )
    if any(
        not isinstance(config.get(key), str) or not config[key].strip()
        for key in required
    ):
        raise ValueError("incomplete V2 signal ingress configuration")
    ages = []
    for key in ("V2_SIGNAL_MAX_AGE_MS", "V2_SIGNAL_MAX_LIFETIME_MS"):
        value = config[key]
        if not value.isascii() or not value.isdigit() or len(value) > 8:
            raise ValueError("invalid V2 signal freshness configuration")
        ages.append(int(value))
    connect = connection_factory(
        config["V2_POSTGRES_DSN"], schema=config["V2_POSTGRES_SCHEMA"]
    )
    ingress = SignalIngress(
        Signals(connect),
        source="tv_bridge",
        environment=config["V2_ENVIRONMENT"],
        clock_ms=(lambda: time.time_ns() // 1_000_000)
        if clock_ms is None
        else clock_ms,
        max_age_ms=ages[0],
        max_lifetime_ms=ages[1],
    )
    return TradingViewWebhook(ingress, secret=config["V2_TV_WEBHOOK_SECRET"])
