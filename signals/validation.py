"""Signal payload 校验（P2）：对原始 dict 做结构检查，返回错误列表。

与 models 的区别：本模块面向**未构造的原始 payload**（例如存储层读出的
旧格式数据），供 Adapter 在严格失败前给出可读的错误汇总。
不做任何交易业务校验（阈值/方向合理性属 Decision 层）。
"""
from __future__ import annotations

from signals.normalize import SignalValidationError, normalize_side, normalize_symbol

__all__ = ["validate_signal_payload"]

_REQUIRED = ('symbol', 'side', 'signal_type', 'source', 'strategy')


def validate_signal_payload(data: dict) -> list[str]:
    """校验原始 signal dict，返回错误列表（空列表 = 合法）。"""
    if not isinstance(data, dict):
        return [f'signal payload 必须为 dict，实际 {type(data).__name__}']

    errors: list[str] = []
    for key in _REQUIRED:
        if data.get(key) in (None, ''):
            errors.append(f'{key} 缺失或为空')

    if 'symbol' in data and normalize_symbol(data.get('symbol')):
        errors[:] = [e for e in errors if e != 'symbol 缺失或为空']

    if 'side' in data:
        try:
            normalize_side(data.get('side'))
        except SignalValidationError as exc:
            errors.append(str(exc))

    return errors
