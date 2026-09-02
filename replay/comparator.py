"""DecisionComparator — 定位两个 Journal 决策之间的差异（P1-03）。

比较范围（P1-03 v1，刻意收窄）：
  - gates：数量、顺序、每项 name / stage / passed / value / threshold / reason
  - decision：action / accepted / reason / final_score

明确**不**比较运行环境字段：journal_id / created_at / metadata.hostname / metadata.pid 等
——它们不是业务决策差异（journal_id 不同不能导致 same=False）。

相等语义：JSON 归一化后的精确相等
  - bool 与数字不混同（True != 1）
  - tuple 与 list 归一（序列化约定：JSON 无 tuple 概念）
  - 不引入 epsilon（数值精确比较）
"""
from __future__ import annotations

from journal import DecisionJournal
from replay.models import ComparisonResult, Difference

__all__ = ["DecisionComparator"]


def _values_equal(a, b) -> bool:
    """JSON 语义下的精确相等比较。"""
    if isinstance(a, bool) != isinstance(b, bool):
        return False  # True 与 1 语义不同
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a.keys()) == set(b.keys()) and all(_values_equal(a[k], b[k]) for k in a)
    return a == b


class DecisionComparator:
    """比较原始 Journal 与 Replay Journal，输出 first_difference + 全部差异。"""

    def compare(self, original: DecisionJournal,
                replayed: DecisionJournal) -> ComparisonResult:
        """按 gates → decision 顺序逐字段比较，收集全部差异。"""
        differences: list[Difference] = []

        og, rg = original.gates, replayed.gates
        for i in range(min(len(og), len(rg))):
            differences.extend(self._compare_gate(og[i], rg[i], i))
        if len(og) != len(rg):
            differences.append(Difference(
                path='gates.count', expected=len(og), actual=len(rg),
                reason='gate count mismatch (early-return shape changed)'))
        differences.extend(self._compare_decision(original.decision, replayed.decision))

        return ComparisonResult(
            same=not differences,
            differences=tuple(differences),
            first_difference=differences[0].path if differences else None,
        )

    @staticmethod
    def _compare_gate(og, rg, i: int) -> list[Difference]:
        checks = (
            ('name', og.name, rg.name, 'gate name mismatch'),
            ('stage', og.stage, rg.stage, 'gate stage mismatch'),
            ('passed', og.passed, rg.passed, 'gate result mismatch'),
            ('value', og.value, rg.value, 'gate value mismatch'),
            ('threshold', og.threshold, rg.threshold, 'gate threshold mismatch'),
            ('reason', og.reason, rg.reason, 'gate reason mismatch'),
        )
        return [Difference(path=f'gates[{i}].{name}', expected=exp, actual=act, reason=reason)
                for name, exp, act, reason in checks if not _values_equal(exp, act)]

    @staticmethod
    def _compare_decision(od, rd) -> list[Difference]:
        checks = (
            ('action', od.action, rd.action, 'decision action mismatch'),
            ('accepted', od.accepted, rd.accepted, 'decision accepted mismatch'),
            ('reason', od.reason, rd.reason, 'decision reason mismatch'),
            ('final_score', od.final_score, rd.final_score, 'decision final_score mismatch'),
        )
        return [Difference(path=f'decision.{name}', expected=exp, actual=act, reason=reason)
                for name, exp, act, reason in checks if not _values_equal(exp, act)]
