"""risk/ — Pure Risk Core + Risk Service（V2 Phase 3-04）。

- core.py: 纯 Risk 函数（零 IO），从 shared_executor/position_models 原样迁移
- service.py: 带 IO 编排（balance/Redis/clock 通过参数注入）
- shared_executor 通过 re-export 保持 backward compatibility
"""
from risk.core import (
    AtrRiskPositionSizer,
    bounded_stop_pct,
    leverage_for_score,
    score_to_fraction,
)
from risk.service import (
    calc_position_qty,
    drawdown_mode,
    drawdown_status,
)

__all__ = [
    "AtrRiskPositionSizer",
    "bounded_stop_pct",
    "calc_position_qty",
    "drawdown_mode",
    "drawdown_status",
    "leverage_for_score",
    "score_to_fraction",
]
