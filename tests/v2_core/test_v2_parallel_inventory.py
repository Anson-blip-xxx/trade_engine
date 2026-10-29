"""Independent inventory GETs may overlap; safety boundaries never do."""

import threading
import time
from uuid import uuid4

import pytest
from test_v2_account_inventory import SCOPE, ReadAccount
from test_v2_intent_admission import database as database_fixture

from v2_core.account_coverage import AccountCoverageAudit
from v2_core.account_inventory import AccountInventory
from v2_core.runtime_policy import PolicyStore

database = database_fixture


class ObservedReader(ReadAccount):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.active = self.peak = self.position_reads = 0

    def __call__(self, method, path, params):
        assert method == "GET"
        with self.lock:
            if path == "/fapi/v3/positionRisk":
                assert self.active == 0
                self.position_reads += 1
                return super().__call__(method, path, params)
            assert self.position_reads == 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.03)
            return super().__call__(method, path, params)
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.parametrize("workers", [2, 4])
def test_parallel_inventory_brackets_all_reads(workers):
    reader = ObservedReader()
    result = AccountInventory(
        reader, scope=SCOPE, clock_ms=lambda: 1000, read_workers=workers
    ).collect(str(uuid4()))
    assert result["status"] == "CLEAR_FOR_RECOVERY_CHECKS"
    assert not result["execution_authorized"]
    assert reader.active == 0 and reader.position_reads == 2
    assert reader.peak == workers
    assert len(result["response_digests"]) == 7


def test_parallel_inventory_failure_drains_reads_but_never_reports_clear():
    reader = ObservedReader()
    reader.fail = "/fapi/v1/openOrders"
    result = AccountInventory(
        reader, scope=SCOPE, clock_ms=lambda: 1000, read_workers=4
    ).collect(str(uuid4()))
    assert "INCOMPLETE_ACCOUNT_READ" in result["blockers"]
    assert result["status"] == "BLOCKED" and not result["execution_authorized"]
    assert reader.active == 0 and reader.position_reads == 1
    assert "sensitive" not in str(result)


def test_parallel_inventory_detects_position_change():
    class Moving(ObservedReader):
        def __call__(self, method, path, params):
            result = super().__call__(method, path, params)
            if path == "/fapi/v3/positionRisk" and self.position_reads == 2:
                return [
                    {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"}
                ]
            return result

    result = AccountInventory(
        Moving(), scope=SCOPE, clock_ms=lambda: 1000, read_workers=4
    ).collect(str(uuid4()))
    assert "POSITION_CHANGED_DURING_INVENTORY" in result["blockers"]


def test_parallel_inventory_checks_deadline_before_any_read():
    times = iter([1000, 40000, 40000])
    reader = ReadAccount()
    result = AccountInventory(
        reader, scope=SCOPE, clock_ms=lambda: next(times), read_workers=4
    ).collect(str(uuid4()))
    assert "ACQUISITION_DEADLINE" in result["blockers"]
    assert reader.calls == []


def test_coverage_reads_current_concurrency_policy(database):
    audit = AccountCoverageAudit(
        database, ReadAccount(), scope=SCOPE, clock_ms=lambda: 1000
    )
    assert audit.inventory.read_workers() == 4
    PolicyStore(database, SCOPE).patch(
        {"context.read_workers": 1}, expected_version=0, reason="QA rollback"
    )
    assert audit.inventory.read_workers() == 1


@pytest.mark.parametrize("workers", [0, 5, True, lambda: 0])
def test_invalid_worker_setting_fails_before_requests(workers):
    reader = ReadAccount()
    with pytest.raises(ValueError):
        AccountInventory(
            reader, scope=SCOPE, clock_ms=lambda: 1000, read_workers=workers
        ).collect(str(uuid4()))
    assert reader.calls == []
