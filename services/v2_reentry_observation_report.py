"""Bounded research-table reads; scope authorization is the console's job."""

from dataclasses import asdict

from psycopg.types.json import Jsonb

from services.v2_reentry_evaluation import coverage_report, evaluation_summary, outcomes


def observation_report(conn, registry, scope, *, days, now):
    if type(days) is not int or days not in {1, 7, 14, 30, 90}:
        raise ValueError("ANALYSIS_WINDOW_INVALID")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("SHADOW_READ_ONLY_REQUIRED")
    base = {
        "kind": "ASYNCHRONOUS_DECISION_SHADOW",
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "simulated_pnl": None,
        "rows": [],
        "health": {"status": "NOT_INSTALLED"},
        "truncated": False,
        "coverage": {"status": "UNAVAILABLE"},
        "evaluation": evaluation_summary([]),
        "limitations": [
            "仅旁路观察启动后、新产生的 OPEN 决策，不阻断真实开仓；READY 不代表获准下单或预期盈利。",
            "只扫描最近观测窗口；停机超过窗口、限额或积压可能遗漏，不能声称全量覆盖。",
            "行情来自观测时已有归档，不证明原下单前已收到；缺失、超时与前序不明单独标记。",
            "保存冻结判断和研究机会去重，不写交易账户状态；不把过滤后的原单收益当作回测收益。",
            "结果只关联所展示最近 100 条的原交易：完整成交、完全平仓且最终结算证据齐备才计入胜率；未结算不按亏损或零收益处理。",
            "按影子参数、原策略版本、来源、方向和研究去重结果分组，不混算参数版本；WAIT 后实际盈利只是待复核样本，不证明反事实误拦率。",
            "覆盖核对仅查看最近 5 分钟数据库新写入的 OPEN，最多 500 条；未匹配可能来自启动、账户切换、停机或限额，不等于真实交易被阻断。",
        ],
    }
    if scope.environment != "SANDBOX":
        base["health"] = {"status": "SANDBOX_ONLY"}
        return base
    if (
        conn.execute("SELECT to_regclass('v2_reentry_observations')").fetchone()[0]
        is None
    ):
        return base
    base["health"] = {"status": "NOT_STARTED"}
    state = conn.execute(
        "SELECT started_ms,heartbeat_ms,health FROM v2_reentry_observer_state WHERE name='sandbox'"
    ).fetchone()
    if state:
        if state[2].get("registry_id") == registry:
            base["health"] = {
                **state[2],
                "started_ms": state[0],
                "heartbeat_ms": state[1],
                "stale": now - state[1]
                > max(60000, state[2].get("limits", {}).get("poll_seconds", 15) * 3000),
            }
        else:
            base["health"] = {"status": "NOT_OBSERVING_SELECTED_ACCOUNT"}
    rows = conn.execute(
        """SELECT decision_id::text,decided_ms,observed_ms,symbol,side,status,reason,claim,
      payload->'assessment'->>'path',payload->'candidate'->>'source',policy_digest,
      payload->'candidate'->>'config_digest',payload->'candidate'->>'strategy_version'
      FROM v2_reentry_observations WHERE registry_id=%s AND scope=%s
      AND observed_ms BETWEEN %s AND %s ORDER BY observed_ms DESC,decision_id DESC LIMIT 101""",
        (registry, Jsonb(asdict(scope)), max(0, now - days * 86400000), now),
    ).fetchall()
    keys = (
        "decision_id",
        "decided_ms",
        "observed_ms",
        "symbol",
        "side",
        "status",
        "reason",
        "claim",
        "path",
        "source",
        "policy_digest",
        "config_digest",
        "strategy_version",
    )
    base["rows"] = [dict(zip(keys, r)) for r in rows[:100]]
    base["truncated"] = len(rows) > 100
    actual = outcomes(conn, scope, [r["decision_id"] for r in base["rows"]], now=now)
    for row in base["rows"]:
        row["actual"] = actual[row["decision_id"]]
    base["evaluation"] = evaluation_summary(base["rows"])
    base["coverage"] = coverage_report(
        conn, registry, scope, health=base["health"], now=now
    )
    return base
