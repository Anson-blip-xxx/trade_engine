"""P9-10 closure inventory guards."""
from pathlib import Path


CLOSURE = Path('docs/v2/PHASE9_CLOSURE.md')

FINAL_STATUS = {
    'S0-1': 'FIXED / CLOSED',
    'PMB-9': 'FIXED / CLOSED',
    'PMB-17': 'FIXED / CLOSED',
    'PMB-23': 'INVALID / SUPERSEDED',
    'PMB-23A': 'DEFERRED / NEEDS_PRODUCT_DECISION',
    'PMB-23B': 'FIXED / CLOSED',
    'PMB-24': 'DEFERRED / NEEDS_PRODUCT_DECISION',
    'PMB-26': 'INVALID / SUPERSEDED',
    'PMB-26A': 'REVIEWED / INTENTIONAL / CLOSED',
    'PMB-26B': 'REVIEWED / INTENTIONAL / CLOSED',
    'PMB-26B2': 'DEFERRED / NEEDS_PRODUCT_DECISION',
    'PMB-26C': 'INVALID / SUPERSEDED',
    'PMB-26C1': 'DEFERRED / NEEDS_PRODUCT_DECISION',
    'PMB-26C2': 'DEFERRED / NEEDS_PRODUCT_DECISION',
    'PMB-27': 'FIXED / CLOSED',
    'PMB-29': 'NO-RISK / KEEP',
    'PMB-30': 'FIXED / CLOSED',
    'T1': 'INVALID / SUPERSEDED',
    'T1-A': 'FIXED / CLOSED',
    'T1-B': 'DEFERRED / NEEDS_PRODUCT_DECISION',
    'T5': 'DEFERRED / DESIGN_REQUIRED',
    'T12': 'DEFERRED / DESIGN_REQUIRED',
    'T14': 'INVALID / SUPERSEDED',
    'POS-ID': 'DEFERRED / DESIGN_REQUIRED',
    'PMB-4': 'DEFERRED / DESIGN_REQUIRED',
}

PRODUCTION_COMMITS = {
    '8ee0ea5': 'strategies/shared_executor.py',
    'f1d3cd2': 'shared/position_manager.py',
    'e4a4a2a': 'position_lifecycle/service.py',
    'eb6c14f': 'position_lifecycle/service.py',
    '11d5283': 'position_reconcile/service.py',
    '32412d9': 'position_monitoring/service.py',
}


def _inventory_rows(source):
    block = source.split('<!-- FINAL_STATUS_START -->', 1)[1].split(
        '<!-- FINAL_STATUS_END -->', 1)[0]
    rows = {}
    for line in block.splitlines():
        if not line.startswith('| ') or line.startswith('|---'):
            continue
        cells = [cell.strip() for cell in line.strip('|').split('|')]
        if cells[0] != 'Ticket':
            rows[cells[0]] = cells[1]
    return rows


def test_final_ticket_inventory_has_no_unknown_status():
    rows = _inventory_rows(CLOSURE.read_text())
    assert rows == FINAL_STATUS


def test_production_commit_and_rollback_ledgers_are_complete():
    source = CLOSURE.read_text()
    for commit, production_file in PRODUCTION_COMMITS.items():
        assert f'`{commit}`' in source
        assert f'`{production_file}`' in source
        assert f'`git revert {commit}`' in source


def test_closure_decision_and_zero_diff_scope_are_explicit():
    source = CLOSURE.read_text()
    assert source.count('PHASE 9 READY TO CLOSE') >= 2
    assert 'production diff is zero' in source
    assert 'There are no `OPEN / READY`' in source
