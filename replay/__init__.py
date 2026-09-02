"""replay/ — Decision Journal 回放与比较基础设施（V2 P1-03）。

职责（Mode A · Recorded Replay）：
    Journal → validate → 序列化 round-trip → compare → ReplayResult

- ReplayRunner：确定性回放入口（replay / replay_dict / replay_json）
- DecisionComparator：gates + decision 逐字段比较，输出 first_difference
- Difference / ComparisonResult / ReplayResult：frozen 数据模型

边界：
- 纯内存只读，无任何 IO（网络/Redis/DB/Binance/文件）
- 不重新执行 S6/S8（Mode B Strategy Replay 属后续阶段，Comparator 为其预留）
- 不接 Recorder / Writer（Journal 持久化属后续阶段）

字段说明见 docs/v2/DECISION_JOURNAL_SCHEMA.md；Journal 模型见 journal/。
"""
from replay.comparator import DecisionComparator
from replay.models import ComparisonResult, Difference, ReplayResult
from replay.runner import ReplayRunner

__all__ = [
    "ComparisonResult",
    "DecisionComparator",
    "Difference",
    "ReplayResult",
    "ReplayRunner",
]
