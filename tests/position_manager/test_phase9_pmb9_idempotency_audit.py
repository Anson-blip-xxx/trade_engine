"""P9-03A：T1/PMB-30 idempotency audit（只 freeze current + 意图边界）。"""
from __future__ import annotations

import threading

import pytest

import shared.position_manager as pm


def _pos(**over):
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S6', 'open_time': 111.0, 'sl': 1.5}
    pos.update(over)
    return pos


class TestCurrentOrderingFrozen:
    def test_order_and_enqueue_before_duplicate(self):
        """OBS-3 frozen：real order + ENQUEUE 先于 dup-check。"""
        src = open('position_lifecycle/service.py').read()
        assert "fapi_post('/fapi/v1/order'" in src
        assert ('self.protection.enq(symbol'
                in src)               # enqueue 在
        # P9-03B：dup precheck (load) 已前移至 enqueue 之前
        assert src.index('positions = self.state.load()') \
            < src.index('self.protection.enq(')
        assert 'if symbol in positions' in src

    def test_dupcheck_before_side_effects_fixed(self):
        """P9-03B regression：precheck 在 leverage/order/enqueue 之前。"""
        src = open('position_lifecycle/service.py').read()
        dup_idx = src.index('if symbol in positions')
        assert dup_idx < src.index("fapi_post('/fapi/v1/leverage'")
        assert dup_idx < src.index("fapi_post('/fapi/v1/order'")
        assert dup_idx < src.index('self.protection.enq(')

    def test_duplicate_return_contract_true(self, monkeypatch, fake_redis):
        pm2 = pm
        monkeypatch.setattr(pm2, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm2, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm2, '_was_closed_recently', lambda s: False)
        monkeypatch.setattr(pm2, '_algo_start_worker', lambda: None)
        monkeypatch.setattr(pm2, '_algo_enqueue', lambda *a: None)
        monkeypatch.setattr(pm2, '_s6api', lambda: (
            None, lambda p, q=None: {'ok': 1}, None, None, None,
            None, None, None))
        monkeypatch.setattr(pm2, '_load', lambda: {
            'TUSDT': _pos()})
        ok = pm2.open_position('TUSDT', 'SHORT', 2.0, 10.0, 3, 1.9,
                              system='S6')
        assert ok is True                     # idempotent-success 合同


class TestPMB30OrphanFact:
    def test_orphan_sl_resolved_on_dup(self, fake_redis, monkeypatch):
        """P9-03B：dup path 已零 side effect（PMB-30 duplicate branch resolved）。
        precheck 在 0 IO 之前 → 0 order / 0 enqueue / 0 save / return True。"""
        enq, orders = [], []
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
        worker_started = []
        monkeypatch.setattr(pm, '_algo_start_worker',
                            lambda: worker_started.append(1))
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda s, side, sl, q: enq.append((s, side, sl, q)))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, lambda p, q=None: orders.append(1) or {'ok': 1}, None,
            None, None, None, None, None))
        saved = []
        monkeypatch.setattr(pm, '_load', lambda: {'TUSDT': _pos()})
        monkeypatch.setattr(pm, '_save', lambda p: saved.append(p))
        ok = pm.open_position('TUSDT', 'SHORT', 2.0, 10.0, 3, 1.9)
        assert ok is True                       # return 合同不变
        assert enq == [] and orders == [] and saved == []
        assert worker_started == []              # worker 0 side effect


class TestConcurrencyLimitationGuard:
    def test_no_open_scoped_lock_exists(self):
        """precheck != concurrency-safe idempotency：无 open-scoped lock。"""
        src = open('shared/position_manager.py').read()
        for token in ("pm:open:{sym}", 'pm:open_lock',
                      'open-scoped lock'):
            assert token not in src           # 保持 zero union
        src2 = open('position_lifecycle/service.py').read()
        assert 'lock' not in src2            # 也没有 open-scoped lock


class TestSameSymbolOppositeSide:
    def test_one_way_side_blind_semantics_frozen(self):
        """strategies 层 `has_any_position` side-blind 语义（one-way mode）。"""
        src = open('strategies/shared_executor.py').read()
        assert 'Binance one-way mode: any side blocks a new strategy order' in src

    def test_pm_lifecycle_key_is_symbol_not_side(self):
        src = open('position_lifecycle/service.py').read()
        assert 'if symbol in positions' in src
        dp = src.index('if symbol in positions')
        line = src[dp:src.index('\n', dp)]
        assert 'side' not in line    # PM 层 dup-check 不含 side 板


class TestStrategyLayerGate:
    def test_strategy_layer_blocks_before_pm_dup(self):
        """evidence：S6/S8 `_has_pos`（existing-position gate）在 PM open 之前。"""
        s6 = open('strategies/S6.py').read()
        s8 = open('strategies/S8.py').read()
        assert 'existing_position' in s6 and 'existing_position' in s8
        assert 'has_any_position(symbol)' in s6
        assert 'has_any_position(symbol)' in s8

    def test_has_position_system_prefix(self):
        """`has_position` per-system prefix 真实实现。"""
        src = open('strategies/shared_executor.py').read()
        assert "p.get('system', '').startswith(name)" in src


class TestRetryAfterSaveFailure:
    def test_save_failure_repeats_order_semantics_documented(self, fake_redis,
                                                             monkeypatch):
        """save 失败后同请求 retry → 重复 real order + dup SL（documented）。"""
        pm2 = pm
        monkeypatch.setattr(pm2, '_rget', fake_redis.get)
        monkeypatch.setattr(pm2, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm2, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm2, '_was_closed_recently', lambda s: False)
        enq = []
        monkeypatch.setattr(pm2, '_algo_start_worker', lambda: None)
        monkeypatch.setattr(pm2, '_algo_enqueue',
                            lambda s, side, sl, q: enq.append(1))
        monkeypatch.setattr(pm2, '_s6api', lambda: (
            None, lambda p, q=None: {'ok': 1}, None, None, None,
            None, None, None))
        # save 失败：silently swallowed（P8-01/2 state seal behavior）
        monkeypatch.setattr(pm2, '_load', lambda: {})
        monkeypatch.setattr(pm2, '_save', lambda p: None)
        ok = pm2.open_position('NEWG2', 'SHORT', 2.0, 10.0, 3, 1.9,
                               system='S6')
        assert ok is True and enq == [1]
        # 如果同一请求 retry：local state 仍 empty → 重 open → 再 enq
        ok = pm2.open_position('NEWG2', 'SHORT', 2.0, 10.0, 3, 1.9,
                               system='S6')
        assert ok is True
        # enq 现在是 [1, 1] → 两次 SL enqueue（记录 limitation）
        assert len(enq) == 2                  # 原语义（不修）
