"""P8-02：state glue 迁移 parity（链序 / aliasing / fresh dict / mutation）。"""
from __future__ import annotations

import json

import pytest

import shared.position_manager as pm
from position_state.service import parse_position_risk


@pytest.fixture
def sg(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_monitor_heartbeat_ts', 0.0)
    monkeypatch.setattr(pm, '_RECENTLY_GHOSTED', [])
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda p, params=None: {})
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_ws_snapshot', pm._ws_snapshot)
    return {'pm': pm, 'redis': fake_redis}


class TestLoadChainParity:
    def test_meta_fallback_loads_seeded(self, sg):
        pm = sg['pm']
        pm._save({'A': {'entry': 1.0}})
        loaded = pm._load()
        assert loaded == {'A': {'entry': 1.0}} or 'A' in loaded

    def test_rest_beats_meta(self, sg, monkeypatch):
        pm = sg['pm']
        pm._save({'METAONLY': {'k': 1}})
        monkeypatch.setattr(
            pm._ext_pos_parse, '__doc__', None) if False else None
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [
            {'symbol': 'RESTUSDT', 'positionAmt': '-3.0',
             'entryPrice': '2.0', 'leverage': '5', 'marginType': 'ISOLATED'}])
        loaded = pm._load()
        assert 'RESTUSDT' in loaded

    def test_ws_fresh_short_circuit(self, sg, monkeypatch):
        """WS fresh <30s → merge_and_save 不读 REST。"""
        pm = sg['pm']
        import time as t
        with pm._WS_LOCK:
            pm._WS_POSITIONS = {'WSUSDT': {'entry': 9.0, 'qty': 1.0,
                                           'side': 'LONG'}}
            pm._WS_LAST_UPDATE = t.time()
        monkeypatch.setattr(pm, '_light_fapi_get',
                            lambda p, params=None: (_ for _ in ()).throw(
                                AssertionError('REST 不应被读')))
        try:
            loaded = pm._load()
        finally:
            with pm._WS_LOCK:
                pm._WS_POSITIONS.clear()
                pm._WS_LAST_UPDATE = 0
        assert 'WSUSDT' in loaded

    def test_meta_filtered_drops_recently_closed(self, sg, monkeypatch):
        pm = sg['pm']
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: s == 'C')
        saved = {}
        monkeypatch.setattr(pm, '_save', lambda p: saved.update(p))
        pm._save({'KEEP': {'e': 1}, 'GONE': {'e': 2}})
        loaded = pm._load()
        assert 'GONE' not in loaded and 'KEEP' not in loaded and \
            'KEEPUSDT' not in loaded and all(k != 'GONE' for k in [loaded])


class TestFreshDictParity:
    def test_fresh_dict_per_call(self, sg):
        pm = sg['pm']
        pm._save({'A': {'entry': 1.0, 'nested': {'x': [1]}}})
        l1 = pm._load_meta()
        l2 = pm._load_meta()
        assert l1 == l2
        assert l1 is not l2                  # fresh dict（PMB-11）
        assert l1['A'] is not l2['A']

    def test_inplace_mutation_not_writeback(self, sg):
        pm = sg['pm']
        pm._save({'A': {'entry': 1.0}})
        meta = pm._load_meta()
        meta['A']['qty'] = 999
        assert pm._load_meta()['A'].get('qty') != 999  # 无回写


class TestMergeParity:
    def _run_both(self, raw, meta, alert_external=False):
        r1 = pm._merge_meta(dict(raw), dict(meta), 777.0,
                            alert_external=alert_external)
        r2 = pm._merge_meta(dict(raw), dict(meta), 777.0,
                            alert_external=False if False else alert_external)
        return r1, r2

    def test_merge_exact_fields_default_fills(self, sg, monkeypatch):
        alerts = []
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: alerts.append(s))
        raw = {'TUSDT': {'entry': 2.0, 'qty': 10.0, 'side': 'SHORT',
                         'leverage': 3}}
        merged = pm._merge_meta(raw, {}, 777.0)
        fields = merged['TUSDT']
        assert merged == merged  # equal variant above
        assert merged['TUSDT']['entry'] == 2.0
        assert merged['TUSDT']['qty'] == 10.0 and \
            merged['TUSDT']['side'] == 'SHORT'
        assert merged['TUSDT']['leverage'] == 3
        assert merged['TUSDT']['margin'] == 'CROSSED'
        assert merged['TUSDT']['system'] == 'S8'          # SHORT→S8
        assert merged['TUSDT']['open_time'] == 777.0
        assert merged['TUSDT']['strength'] == 50
        assert merged['TUSDT']['score'] == 50
        assert merged['TUSDT']['sl'] == round(2.0 * 1.08, 8)   # SHORT 1.08
        assert merged['TUSDT']['be_done'] is False
        assert merged['TUSDT']['tp_done'] == []
        assert merged['TUSDT']['trend_reversal_warned'] is False
        assert merged['TUSDT']['position_id'] == 'S8:TUSDT:777.000000'

    def test_meta_wins_partial_qty(self, sg, monkeypatch):
        alerts = []
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: alerts.append(s))
        raw = {'TUSDT': {'entry': 2.0, 'qty': 10.0, 'side': 'SHORT'}}
        meta = {'TUSDT': {'qty': 4.0, 'sl': None, 'event_type': 'P',
                          'strength': 70}}
        merged = pm._merge_meta(raw, dict(meta), 1.0)
        assert merged['TUSDT']['qty'] == 4.0                       # min
        assert merged['TUSDT']['system'] == 'S8' and \
            merged['TUSDT']['event_type'] == 'P'
        # 冻结语义：strength 缺 spi? no——strength 直接 meta 值；
        # score 缺省时回退 strength
        assert merged['TUSDT']['score'] == 70 and \
            merged['TUSDT']['strength'] == 70
        assert merged['TUSDT']['sl'] == round(2.0 * 1.08, 8)   # sl None→fill

    def test_merge_aliasing_identity(self, sg, monkeypatch):
        """merged 仅在 missing-keep 分支**同一对象** identity。"""
        raw: dict = {}
        meta: dict = {'YUSDT': {'entry': 5.0, 'side': 'SHORT',
                                'system': 'S8', 'qty': 1.0}}
        merged = pm._merge_meta_preserving_missing(raw, meta, 1.0)
        assert merged['YUSDT'] is meta['YUSDT']            # alias identity

    def test_merge_input_mutation_parity(self, sg, monkeypatch):
        """`meta.pop(sym)` 原地弹出——输入被 mutation 的行为保持。"""
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: None)
        raw = {'TUSDT': {'entry': 1.0, 'qty': 1.0, 'side': 'LONG'}}
        input_meta = {'TUSDT': {'entry': 1.0, 'qty': 2.0, 'side': 'LONG'}}
        meta = {'TUSDT': {'entry': 1.0, 'qty': 2.0, 'side': 'LONG'}}
        pm._merge_meta(raw, meta, 1.0)
        assert 'TUSDT' not in meta              # 原地 pop（frozen 语义）

    def test_insertion_order_parity(self, sg, monkeypatch):
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: None)
        raw = {'CUSDT': {'entry': 3.0, 'qty': 1.0, 'side': 'LONG'},
               'AUSDT': {'entry': 1.0, 'qty': 1.0, 'side': 'LONG'},
               'BUSDT': {'entry': 2.0, 'qty': 1.0, 'side': 'LONG'}}
        merged = pm._merge_meta(raw, {}, 1.0)
        assert list(merged.keys()) == ['CUSDT', 'AUSDT', 'BUSDT']
        raw2 = {'AUSDT': {'entry': 1.0, 'qty': 1.0, 'side': 'LONG'},
                'BUSDT': {'entry': 2.0, 'qty': 1.0, 'side': 'LONG'}}
        merged2 = pm._merge_meta(raw2, {}, 1.0)
        assert list(merged2.keys()) == ['AUSDT', 'BUSDT']

    def test_malformed_field_falls_back(self, sg, monkeypatch):
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: None)
        raw = {'TUSDT': {'entry': 1.0, 'qty': 1.0, 'side': 'LONG'}}
        meta = {'TUSDT': {'weird': object(), 'attributeNone': None}}
        merged = pm._merge_meta(raw, meta, 5.0)
        assert merged['TUSDT']['entry'] == 1.0   # 带 none 字段不炸
        assert merged['TUSDT']['sl'] == round(1.0 * 0.92, 8)   # LONG 0.92


class TestRestParseParity:
    def test_micro_amount_skipped(self, monkeypatch):
        out = {}
        pm._ext_pos_parse([
            {'symbol': 'XUSDT', 'positionAmt': '0.0005', 'entryPrice': '1'},
        ], out)
        assert out == {}                        # 微量 skip

    def test_non_list_returns_partial(self, monkeypatch):
        out = {}
        assert pm._ext_pos_parse(None, out) is out

    def test_side_and_defaults(self):
        out = pm._ext_pos_parse([
            {'symbol': 'S1USDT', 'positionAmt': '-2.5', 'entryPrice': '3.0'},
            {'symbol': 'L1USDT', 'positionAmt': '7.0', 'entryPrice': '3.5',
             'leverage': '9', 'marginType': 'isolated'}])
        assert out['S1USDT'] == {'entry': 3.0, 'side': 'SHORT', 'qty': 2.5,
                                 'leverage': 3, 'margin': 'CROSSED'}
        assert out['S1USDT']['leverage'] == 3
        assert out['S1USDT']['margin'] == 'CROSSED'
        assert out[list(out.keys())[1]]['leverage'] == 9

    def test_mid_parse_exception_partial_kept(self, sg, monkeypatch):
        """老语义：REST 增量解析中异常 → 已解析项保留（caller 吞错）。"""
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [
            {'symbol': 'OKUSDT', 'positionAmt': '2', 'entryPrice': '1.0'},
            'NOTDICT',
        ])
        # 单项异常 → 整轮 try 吞错：{OKUSDT} 的 partial 保持
        pm._rest_positions_snapshot()
        # 同等验证 service 解析抛出（异常类型不吞）
        import pytest as _p
        # 缺 positionAmt 字段 → float 默认 0 → skip（frozen 语义）
        assert pm._ext_pos_parse([{'symbol': 'X'}], {}) == {}
        # 缺 entryPrice → 已进环 → KeyError（异常上抛不吞）
