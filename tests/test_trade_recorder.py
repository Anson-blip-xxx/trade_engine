"""trade_recorder 双进程重复记账防御（D 档）回归测试。"""
from unittest.mock import patch

from shared.trade_recorder import _is_duplicate_record


def test_duplicate_detected_within_window():
    """2 分钟内已有同 symbol+entry+qty+exit_reason 记录 → 判重。"""
    with patch('shared.trade_recorder._ch_query', return_value=[[1]]) as m:
        assert _is_duplicate_record('RIFUSDT', 0.09314, 11829, '时间止损') is True
        sql = m.call_args[0][0]
        assert 'RIFUSDT' in sql and '0.09314' in sql and '11829' in sql and '时间止损' in sql


def test_no_duplicate():
    """无重复 → 不判重。"""
    with patch('shared.trade_recorder._ch_query', return_value=[[0]]):
        assert _is_duplicate_record('BTCUSDT', 60000.0, 0.05, '手动平仓') is False


def test_duplicate_ch_query_error_safe():
    """CH 查询异常时降级为不判重（不阻断正常记账）。"""
    with patch('shared.trade_recorder._ch_query', side_effect=Exception('conn refused')):
        assert _is_duplicate_record('BTCUSDT', 60000.0, 0.05, '手动平仓') is False


def test_duplicate_sql_escapes_quotes():
    """exit_reason 含单引号时转义，避免 SQL 注入。"""
    with patch('shared.trade_recorder._ch_query', return_value=[[0]]) as m:
        _is_duplicate_record('XX', 1.0, 2.0, "it's")
        sql = m.call_args[0][0]
        assert "it''s" in sql
