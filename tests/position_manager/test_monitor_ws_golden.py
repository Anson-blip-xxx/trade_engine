"""P7-05A：WS ACCOUNT_UPDATE 快照 golden（外加删仓/标 closed 边界）。

冻结:
- 只处理 'e' == 'ACCOUNT_UPDATE'（心跳/操作事件直接丢弃）
- |pa| < 0.001 → pop；prev 存在且未 closed → log『WS平仓』+ record ghost
  → _mark_closed（先记账后标记，跨进程去重）
- 新仓 schema：entry/side/qty/leverage/margin/system='?'/open_time/sl=0
  /be_done=False 逐字
- _WS_LAST_UPDATE 无论内容如何都在锁内刷新
- decode 全程 try/except：异常只 log『[WS消息异常]』不 raise
"""
import json
import time

import pytest

from shared import position_manager as pm


@pytest.fixture
def ws(monkeypatch):
    pm._WS_POSITIONS = {}
    pm._WS_LAST_UPDATE = 0.0
    calls = {'ghost': [], 'mark': [], 'logs': []}
    def _ghost(s, p):
        calls['ghost'].append((s, dict(p)))
        return True
    monkeypatch.setattr(pm, '_try_record_ghost_trade', _ghost)
    monkeypatch.setattr(pm, '_mark_closed',
                        lambda s: calls['mark'].append(s))
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    monkeypatch.setattr(pm, '_pmlog', lambda m: calls['logs'].append(str(m)))
    return {'pm': pm, 'calls': calls}


def _upd(pa):
    return json.dumps({'e': 'ACCOUNT_UPDATE',
                       'a': {'P': [pa]}})


class TestNonAccountUpdate:
    def test_non_account_update_dropped(self, ws):
        ws['pm']._ws_on_message(None, json.dumps({'e': 'MARGIN_CALL'}))
        assert ws['pm']._WS_POSITIONS == {}
        assert ws['pm']._WS_LAST_UPDATE == 0.0   # 也不刷新

    def test_malformed_json_logged(self, ws):
        ws['pm']._ws_on_message(None, 'not-json{')
        assert any('WS消息异常' in l for l in ws['calls']['logs'])
        assert ws['pm']._WS_POSITIONS == {}


class TestPositionUpsert:
    def test_schema_frozen(self, ws):
        ws['pm']._ws_on_message(
            None, _upd({'s': 'TUSDT', 'pa': '5.0', 'ep': '1.5',
                        'lev': '5', 'mt': 'cross'}))
        p = ws['pm']._WS_POSITIONS['TUSDT']
        assert p == {'entry': 1.5, 'side': 'LONG', 'qty': 5.0,
                     'leverage': 5, 'margin': 'CROSS', 'system': '?',
                     'open_time': time.time() and p['open_time'],
                     'sl': 0, 'be_done': False}

    def test_negative_amt_short(self, ws):
        ws['pm']._ws_on_message(
            None, _upd({'s': 'TUSDT', 'pa': '-3.0', 'ep': '2.0'}))
        assert ws['pm']._WS_POSITIONS['TUSDT']['side'] == 'SHORT'

    def test_last_update_refreshed(self, ws):
        ws['pm']._ws_on_message(
            None, _upd({'s': 'TUSDT', 'pa': '1.0', 'ep': '1.0'}))
        assert ws['pm']._WS_LAST_UPDATE > 0

    def test_overwrite_existing_symbol(self, ws):
        ws['pm']._ws_on_message(
            None, _upd({'s': 'TUSDT', 'pa': '1.0', 'ep': '1.0'}))
        ws['pm']._ws_on_message(
            None, _upd({'s': 'TUSDT', 'pa': '2.0', 'ep': '0.9'}))
        assert ws['pm']._WS_POSITIONS['TUSDT']['qty'] == 2.0


class TestWsClose:
    def test_close_pops_and_records_ghost(self, ws):
        ws['pm']._WS_POSITIONS['TUSDT'] = {
            'entry': 1.0, 'side': 'LONG', 'qty': 1.0}
        ws['pm']._ws_on_message(None, _upd({'s': 'TUSDT', 'pa': '0'}))
        assert 'TUSDT' not in ws['pm']._WS_POSITIONS
        assert ws['calls']['ghost'] == [
            ('TUSDT', {'entry': 1.0, 'side': 'LONG', 'qty': 1.0})]
        assert ws['calls']['mark'] == ['TUSDT']
        assert any('WS平仓' in l and 'AlgoSL' in l
                   for l in ws['calls']['logs'])

    def test_close_when_recently_marked_skip_recording(self, ws, monkeypatch):
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: True)
        ws['pm']._WS_POSITIONS['TUSDT'] = {'entry': 1.0, 'side': 'LONG',
                                           'qty': 1.0}
        ws['pm']._ws_on_message(None, _upd({'s': 'TUSDT', 'pa': '0'}))
        assert ws['calls']['ghost'] == []    # 已用 recorded → 双进程去重
        assert ws['calls']['mark'] == []
        assert 'TUSDT' not in ws['pm']._WS_POSITIONS  # 仓位本身仍被移除
