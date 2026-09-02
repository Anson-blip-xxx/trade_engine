"""ReplayRunner — Recorded Journal Replay（P1-03 · Mode A）。

对单个 DecisionJournal 做确定性回放：

    Journal → validate → serialize round-trip（to_dict → from_dict）→ compare → ReplayResult

它验证三件事：
  1. Journal 结构合法（schema 必须字段 / gate stage 从 1 连续 / decision 存在）
  2. 序列化 round-trip 无损（Journal 事实可以被稳定回放）
  3. 决策与 gates 可稳定比较并定位差异

边界（P1-03）：
  - 纯内存、只读：无网络 / Redis / DB / Binance / 文件 IO，不下单不改仓位
  - 不重新执行 S6/S8 策略（Mode B 属后续阶段；Comparator 即为 Mode B 预留的差异引擎）
  - 预期输入错误转换为 replayed=False 的 ReplayResult，不 crash、不静默修复
"""
from __future__ import annotations

import json

from journal import (
    DECISION_JOURNAL_SCHEMA,
    DecisionJournal,
    JournalValidationError,
    from_dict,
    to_dict,
    validate_journal,
)
from replay.comparator import DecisionComparator
from replay.models import ReplayResult

__all__ = ["ReplayRunner"]


class ReplayRunner:
    """确定性 Replay：validate → round-trip → compare → ReplayResult。"""

    def __init__(self):
        self._comparator = DecisionComparator()

    # ── 公共 API ────────────────────────────────────────────────────────
    def replay(self, journal: DecisionJournal) -> ReplayResult:
        """回放一个 DecisionJournal：结构校验 + 序列化 round-trip + 决策比较。"""
        if not isinstance(journal, DecisionJournal):
            return self._error_result(
                None, 'invalid_input',
                f'expected DecisionJournal, got {type(journal).__name__}')

        errors = validate_journal(journal)
        if errors:
            return self._error_result(journal, 'invalid_journal', '; '.join(errors))

        try:
            normalized = from_dict(to_dict(journal))
        except (JournalValidationError, ValueError, TypeError) as exc:
            return self._error_result(journal, 'invalid_journal', str(exc))

        comparison = self._comparator.compare(journal, normalized)
        return self._success_result(journal, normalized, comparison)

    def replay_dict(self, data) -> ReplayResult:
        """从 dict 回放（典型来源：存储层读出的 JSON 文档）。"""
        if not isinstance(data, dict):
            return self._error_result(
                None, 'invalid_input', f'expected dict, got {type(data).__name__}')
        missing = sorted(set(DECISION_JOURNAL_SCHEMA['required']) - set(data.keys()))
        if missing:
            return self._error_result(
                None, 'missing_required_field', f"missing: {', '.join(missing)}")
        try:
            journal = from_dict(data)
        except JournalValidationError as exc:
            jid = data.get('journal_id')
            return self._error_result(None, 'invalid_journal', str(exc),
                                      journal_id=jid if isinstance(jid, str) else None)
        return self.replay(journal)

    def replay_json(self, text) -> ReplayResult:
        """从 JSON 字符串回放。"""
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            return self._error_result(None, 'invalid_input', f'invalid json: {exc}')
        if not isinstance(data, dict):
            return self._error_result(None, 'invalid_input', 'json must be an object')
        return self.replay_dict(data)

    # ── 内部 ────────────────────────────────────────────────────────────
    @staticmethod
    def _decision_fields(journal: DecisionJournal):
        d = journal.decision
        return d.action, d.accepted, d.reason, d.final_score

    def _success_result(self, original: DecisionJournal,
                        replayed: DecisionJournal,
                        comparison) -> ReplayResult:
        oa, oacc, ors, ofs = self._decision_fields(original)
        ra, racc, rrs, rfs = self._decision_fields(replayed)
        return ReplayResult(
            journal_id=original.journal_id,
            replayed=True,
            same=comparison.same,
            original_action=oa, replay_action=ra,
            original_accepted=oacc, replay_accepted=racc,
            original_reason=ors, replay_reason=rrs,
            original_final_score=ofs, replay_final_score=rfs,
            first_difference=comparison.first_difference,
            differences=comparison.differences,
            metadata={'error': None, 'error_detail': None},
        )

    def _error_result(self, journal, error: str, detail: str,
                      journal_id: str | None = None) -> ReplayResult:
        """预期输入错误 → replayed=False 的结果（不 crash、不静默修复）。"""
        if journal is not None:
            oa, oacc, ors, ofs = self._decision_fields(journal)
            jid = journal.journal_id
        else:
            oa = oacc = ors = ofs = None
            jid = journal_id
        return ReplayResult(
            journal_id=jid,
            replayed=False,
            same=False,
            original_action=oa, replay_action=None,
            original_accepted=oacc, replay_accepted=None,
            original_reason=ors, replay_reason=None,
            original_final_score=ofs, replay_final_score=None,
            first_difference=None,
            differences=(),
            metadata={'error': error, 'error_detail': detail},
        )
