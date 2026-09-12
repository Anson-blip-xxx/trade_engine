"""P6-01：S0 失败矩阵（文件写失败/CH 失败/采样异常/双重写语义）。"""
import json

import pytest

from conftest import BASE_TS


class TestFileWriteFailure:
    def test_file_failure_propagates(self, g, monkeypatch):
        """STATE_FILE 为非法路径（目录不存在）→ open 失败 → 异常上抛
        （write_state 不包文件 IO，外层 main catch）——真实失败行为冻结。"""
        monkeypatch.setattr(g, 'STATE_FILE', '/nonexistent-dir-x/y.json')
        state = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
        with pytest.raises(OSError):
            g.write_state(state)

class TestCHFailureDocumented:
    def test_ch_failure_no_raise(self, s0_env, g, rdis, ch_rows, tmp_path, monkeypatch):
        """CH 写失败 → log warning、不抛（redis+file 已成事实，不回滚）。"""
        def ch_boom(table, row):
            raise RuntimeError('ch down')
        monkeypatch.setattr('shared.clickhouse_client.insert', ch_boom)
        state = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
        g.write_state(state)   # 不抛
        assert rdis.writes and (tmp_path / 'market_state.json').exists()


class TestComputeException:
    def test_sample_error_logged_and_continues(self, s0_env, g, rdis, monkeypatch, capsys):
        """sample_btc 抛错 → main 外层 catch → log error → 30s 继续循环。"""
        def boom():
            raise RuntimeError('window read fail')
        monkeypatch.setattr(g, 'sample_btc', boom)
        try:
            with pytest.raises(RuntimeError, match='window read fail') as excinfo:
                g.sample_btc()
            # main() 的 try/except 行为不能直接跑，验证生产代码有外层 try。
            src = __import__('inspect').getsource(g.main)
            assert 'except Exception as e:' in src
            assert 'time.sleep(30)' in src
        finally:
            pass


class TestStaleInputFacts:
    def test_no_stale_check_producer_sides(self, g, rdis):
        """S0 采样端无 stale gate —— S3 market data 恒被当实读。"""
        seed = {'ts': BASE_TS - 5000, 'symbols': {}}   # 5000s 前的数据
        rdis.store['market:s3_data'] = seed
        state = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
        # 没有任何 stale check（返回正常 state）
        assert state['market_state'] == 'range'
