"""P9-00：Behavior ticket inventory 完备性 / Golden 引用存在 / 无 prod diff。"""
import os
import subprocess
from pathlib import Path

import pytest

# ticket list（依据 PHASE8_CLOSURE + observations 家族）
TICKETS = {
    'S0-1': ['tests/s0/'],
    'PMB-9': ['tests/position_manager/test_protection_worker_golden.py'],
    'PMB-17': ['tests/position_manager/test_ghost_queue_golden.py',
               'tests/position_manager/test_monitor_all_golden.py'],
    'PMB-23': ['tests/position_manager/test_ghost_local_only_golden.py'],
    'PMB-24': ['tests/position_manager/test_reconcile_input_golden.py'],
    'PMB-26': ['tests/position_manager/test_reconcile_dedup_golden.py',
               'tests/position_manager/test_external_position_golden.py'],
    'PMB-27': ['tests/position_manager/test_lifecycle_ordering_golden.py'],
    'PMB-29': ['tests/position_manager/test_lifecycle_callers_golden.py'],
    'PMB-30': ['tests/position_manager/test_lifecycle_failure_golden.py'],
    'T1':  ['tests/position_manager/test_open_golden.py'],
    'T5':  ['tests/position_manager/test_protection_worker_golden.py'],
    'T12': ['tests/position_manager/test_lifecycle_ordering_golden.py'],
    'POS-ID': ['tests/position_manager/test_edge_cases_golden.py',
               'tests/position_manager/test_merge_golden.py'],
}


class TestTicketListCompleteness:
    def test_doc_lists_all_tickets(self):
        src = Path('docs/v2/PHASE9_BEHAVIOR_PRIORITY.md').read_text()
        for ticket in TICKETS:
            assert ticket in src, ticket

    def test_scoring_model_documented(self):
        src = Path('docs/v2/PHASE9_BEHAVIOR_PRIORITY.md').read_text()
        assert 'RiskScore' in src and 'Money×3' in src
        for field in ('Money', 'Prob', 'Blast', 'Verify', 'Complexity'):
            assert field in src

    def test_first_ticket_recommendation_exists(self):
        src = Path('docs/v2/PHASE9_BEHAVIOR_PRIORITY.md').read_text()
        assert 'P9-01 = S0-1' in src


class TestReferencedGoldensExist:
    @pytest.mark.parametrize('ticket, files', sorted(TICKETS.items()))
    def test_golden_files_exist(self, ticket, files):
        for f in files:
            assert Path(f).exists(), (ticket, f)

    def test_s0_frozen_read_seam(self):
        """S0-1 冻结位：SE 读 market_mode；s0 写 market_state。"""
        se_src = Path('strategies/shared_executor.py').read_text()
        assert "ms.get('market_mode', 'normal')" in se_src
        core_src = Path('s0/core.py').read_text()
        assert '"market_state"' in core_src

    def test_yellow_architecture_separated(self):
        src = Path('docs/v2/PHASE9_BEHAVIOR_PRIORITY.md').read_text()
        assert 'Yellow Architecture 分离' in src


class TestProductionZeroDiff:
    def test_no_production_change_in_this_phase(self):
        r = subprocess.run(['git', 'diff', '1f9eb00', 'HEAD', '--stat'],
                           capture_output=True, text=True,
                           cwd=Path('.').resolve())
        changed = [l for l in r.stdout.splitlines()
                   if '|' in l and '.pyc' not in l and l.strip()]
        # 仅 docs/phase9 tests 允许
        assert all(('docs/' in l or 'tests/' in l) for l in changed), changed
