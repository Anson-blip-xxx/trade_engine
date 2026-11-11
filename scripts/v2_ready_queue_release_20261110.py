"""Exact SANDBOX hot-queue release; flat-only and dry-run by default.

Schema must have passed transactional projection validation first. Existing
lease-read timeout handling is included; no strategy, risk or exit changes.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIOR = Path("/opt/trade-engine-v2/releases/jev-freshness-controller-20261108-v1")
CANDIDATE = Path("/opt/trade-engine-v2/releases/ready-queue-controller-20261110-v1")
EXPECTED = {
    "v2_core/scheduling.py": "87d1cdff590932808232512d47691ed1809022d69779e14b782cd15bc4207172",
    "v2_core/ready_queue.py": "2c08c8511edad0a5e41e05e8424ec18b4c61a0dbbcc627c7ff5c042b3220faa3",
    "db/postgres_v2_core_schema.sql": "f593bc023f8ebd0b5a68e695f6db8fc884437c179a015c9cdfc08ca429443796",
    "db/migrations/20261110_strategy_ready_queue.sql": "a885043abe9ef68120e559087e1ff756b86f7767063a0c065a576506d2d247ab",
}


def main():
    spec = importlib.util.spec_from_file_location(
        "_ready_queue_release", REPO / "scripts/v2_jev_freshness_release_20261108.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release.PRIOR, release.CANDIDATE, release.EXPECTED = PRIOR, CANDIDATE, EXPECTED
    # The staged migration is independently committed and old-runtime compatible.
    # Do not switch application code if its validation was lost or never finished.
    import sys

    sys.path.insert(0, str(CANDIDATE))
    from services.v2_managed_testnet import database
    from v2_core.ready_queue import queue_mode

    if queue_mode(database()) != ("READY", "VALIDATED_TRANSACTIONAL_PROJECTION"):
        raise ValueError("READY_MIGRATION_VALIDATION_REQUIRED")
    return release.main()


if __name__ == "__main__":
    raise SystemExit(main())
