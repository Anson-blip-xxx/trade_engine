"""Explicit Telegram operational sink; bounded I/O, no credential-bearing errors.

Telegram has no idempotency key for sendMessage: delivery is at least once.
Event IDs are visible so a replay can be identified, never used as trade approval.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from uuid import UUID


class TelegramDeliveryError(RuntimeError):
    """Fixed diagnostic, never raw URL, token, message or response."""


def _number(value, places=8):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION") from None
    if not parsed.is_finite():
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION")
    quantum = Decimal(1).scaleb(-places)
    rendered = format(parsed.quantize(quantum).normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _time(value):
    if type(value) is not int or value < 0:
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION")
    utc_plus_8 = timezone(timedelta(hours=8), name="UTC+8")
    return datetime.fromtimestamp(value / 1000, tz=utc_plus_8).strftime(
        "%Y-%m-%d %H:%M:%S UTC+8"
    )


def _trade_text(scope, kind, _event_id, payload):
    common = {
        "symbol",
        "direction",
        "strategy",
        "system_tag",
        "event_type",
        "score",
        "leverage",
        "margin_type",
        "quantity",
    }
    required = common | (
        {
            "entry_price",
            "opening_notional",
            "planned_notional",
            "planned_loss",
            "stop_price",
            "stop_status",
            "fees",
            "opened_at_ms",
            "analysis_reason",
            "required_margin",
        }
        if kind == "TRADE_OPENED"
        else {
            "entry_price",
            "closing_price",
            "opening_notional",
            "gross_pnl",
            "fees",
            "funding",
            "net_pnl",
            "return_pct",
            "risk_multiple",
            "exit_reason",
            "held_ms",
            "opened_at_ms",
            "closed_at_ms",
            "stop_status",
        }
    )
    if kind != "TRADE_OPENED" and "insurance_clear" in payload:
        required.add("insurance_clear")
    if set(payload) != required or any(
        not isinstance(payload[key], (str, int)) for key in required
    ):
        raise TelegramDeliveryError("INVALID_TRADE_NOTIFICATION")
    side = "做多 LONG" if payload["direction"] == "LONG" else "做空 SHORT"
    title = (
        "🟢 V2 TESTNET 开仓成功"
        if kind == "TRADE_OPENED"
        else (
            "✅ V2 TESTNET 平仓盈利"
            if Decimal(str(payload["net_pnl"])) > 0
            else "🔴 V2 TESTNET 平仓完成"
        )
    )
    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━",
        f"标的：{payload['symbol']}  |  {side}",
        f"策略：{payload['strategy']} / {payload['system_tag']}  |  {payload['event_type']}",
        f"评分：{payload['score']}  |  杠杆：{payload['leverage']}x {payload['margin_type']}",
    ]
    if kind == "TRADE_OPENED":
        lines += [
            "",
            "📌 成交",
            f"数量：{_number(payload['quantity'])}",
            f"均价：{_number(payload['entry_price'])}",
            f"实际名义价值：{_number(payload['opening_notional'])} USDT",
            f"计划名义价值：{_number(payload['planned_notional'])} USDT",
            f"预估保证金：{_number(payload['required_margin'])} USDT",
            f"开仓手续费：{payload['fees']}",
            "",
            "🛡 风控与保护",
            f"计划最大损失：{_number(payload['planned_loss'])} USDT",
            f"止损：{_number(payload['stop_price'])}  |  {payload['stop_status']}",
            f"历史调整：{payload['analysis_reason']}",
            "",
            f"时间：{_time(payload['opened_at_ms'])}",
        ]
    else:
        held = int(payload["held_ms"])
        lines += [
            "",
            "📌 成交与结果",
            f"数量：{_number(payload['quantity'])}",
            f"开仓均价：{_number(payload['entry_price'])}",
            f"平仓均价：{_number(payload['closing_price'])}",
            f"开仓名义价值：{_number(payload['opening_notional'])} USDT",
            f"毛收益：{_number(payload['gross_pnl'])} USDT",
            f"手续费：{_number(payload['fees'])} USDT",
            f"资金费/现金调整：{_number(payload['funding'])} USDT",
            *(
                [f"强平保险清算：{_number(payload['insurance_clear'])} USDT"]
                if payload.get("insurance_clear") is not None
                else []
            ),
            f"净收益：{_number(payload['net_pnl'])} USDT",
            f"收益率：{_number(payload['return_pct'], 4)}%",
            f"风险倍数：{_number(payload['risk_multiple'], 4)}R",
            "",
            "🧭 退出说明",
            f"原因：{payload['exit_reason']}",
            f"持仓：{held // 3600000}h {(held % 3600000) // 60000}m {(held % 60000) // 1000}s",
            f"保护单终态：{payload['stop_status']}",
            f"开仓时间：{_time(payload['opened_at_ms'])}",
            f"平仓时间：{_time(payload['closed_at_ms'])}",
        ]
    return "\n".join(lines)


BUSINESS_HEALTH_MESSAGES = {
    "JEV_LOCAL_DAILY_BUDGET_EXHAUSTED": "JEV 本系统共享日额度不足以预留下次调用；这不代表供应商余额不足。没有获得模型意见，不记为模型通过。按账户既定策略降级或跳过，持仓保护和平仓不受此项影响；每日 UTC+8 00:00 使用新额度窗口。",
    "EXECUTION_GUARD_PERMISSION_DENIED": "执行权限检查失败，交易请求在发送前已被拦截。请检查执行进程的数据库角色和绑定锁权限；开仓、保护及平仓都需要正确权限，禁止绕过安全守卫。",
    "EXECUTION_CONTROLLER_BLOCKED": "受管执行进程启动或运行受阻，当前不能确认完整交易链路可用。请检查执行路由的阻塞原因与启动权限自检，不要仅以服务存活判断交易正常。",
    "OPENING_SUBMISSION_STALLED": "近期有多次开仓尝试，但全部在交易所受理前被拒绝。请查看账户交易健康记录中的具体拒单原因；不是没有市场信号，也不会追补过期信号。",
    "OPENING_REJECTION_RATE_HIGH": "近期实际开仓尝试的拒单比例超过配置阈值。请检查账户交易健康记录和交易所回执，不要通过放宽风控掩盖执行故障。",
    "CAPITAL_TRANSFER_OVERDUE": "存在超过配置时限仍未完成配对或已被冲正的划转流水。请核对双边凭证、期初边界与钱包差额；不会自动补造流水，本告警不代表交易权限变更。",
    "ORDER_PROGRESS_STALLED": "订单提交、结果核对或成交回执超过配置时限。请核对交易所当前仓位、挂单与本地订单；未查到订单不代表仓位已平，不要盲目重复下单。",
    "SETTLEMENT_OVERDUE": "已平仓但超过配置时限仍未完成结算。请核对成交、手续费、资金费及账户现金对账。",
    "SIGNAL_CONSUMPTION_LAG": "存在超过配置时限仍未消费的信号。请检查安全阻塞、消费队列与处理速度；过期信号不会追单。",
    "SIGNAL_MAINTENANCE_DEGRADED": "信号后台清理或审计持续异常，请检查数据库查询与维护线程。此告警不等于开仓链路堵塞；过期信号仍禁止追单。",
    "PIPELINE_ENTRY_BLOCKED": "开仓链路持续阻塞超过配置时限。请检查 PostgreSQL 交易健康记录中的阻塞阶段，不要直接放宽风控。",
    "POSITION_SAFETY_BLOCKED": "保护单或退出环节持续阻塞超过配置时限。请优先核实持仓、止损保护和退出回执。",
    "CAPITAL_DRAWDOWN_HALT": "资金模型达到累计回撤硬停阈值或试运行预算耗尽，已禁止新开仓，不会定时自动解锁。原有仓位的止损保护、退出和结算继续运行；不会通过加杠杆补亏损。",
    "BUSINESS_HEALTH_UNAVAILABLE": "业务健康检查失败，当前无法确认订单与链路健康。进程存活不代表交易正常。",
}

_HEALTH_STATUSES = {
    "PREPARED": "待提交",
    "SUBMITTING": "提交结果待确认",
    "UNKNOWN": "结果未知",
    "ACKNOWLEDGED": "已受理待成交核对",
    "FILLED": "已成交待核对",
    "REJECTED": "已拒绝",
    "CANCELLED": "已取消",
    "RECONCILED": "已对账",
    "BLOCKED": "退出检查受阻",
    "RECOVERING_CLOSE": "平仓回执核对中",
    "WAITING_NATIVE_PROTECTION": "原生保护状态待核对",
    "ACTION_DISABLED": "应急退出未启用",
    "REJECTED_CLOSE_RESIDUAL": "平仓未完成／本地残余待核对",
}
_HEALTH_REASONS = {
    "LOCAL_RESIDUAL_REQUIRES_RECONCILIATION": "最新平仓请求被拒绝，台账仍有剩余；需核对交易所",
    "REJECTED_EXIT_AWAITING_FRESH_OBSERVATION": "等待新的退出行情证据",
    "EXIT_PREFLIGHT_CHANGED": "退出前置证据已变化",
    "EXIT_PREFLIGHT_EXPIRED": "退出前置证据已过期",
    "EXIT_IDENTITY_REQUIRES_RECOVERY": "原退出订单尚需对账，禁止重复下单",
    "EMERGENCY_IDENTITY_REQUIRES_RECOVERY": "原应急退出订单尚需对账",
    "CLOSE_RECOVERY_PENDING": "已有平仓订单正在恢复核对",
    "EMERGENCY_PREFLIGHT_CHANGED": "应急退出前置证据已变化",
    "UNCONFIRMED_PROTECTION": "尚未确认保护状态",
    "PROTECTION_CONFIRMATION_GRACE": "保护确认宽限期内",
    "ACCOUNT_BUSY": "账户操作锁繁忙",
    "BUSY": "退出扫描锁繁忙",
    "EMERGENCY_LOSS": "应急止损条件",
    "ADVERSE_FUNDING": "资金费条件",
    "EARLY_LOSS_MOMENTUM_WEAK": "早期亏损且动量转弱",
    "LOW_YIELD_STAGNATION": "低收益停滞",
    "PARTIAL_TAKE_PROFIT": "分批止盈",
    "PEAK_DRAWDOWN": "收益回撤退出",
    "ONE_HOUR_REVERSAL": "趋势反转",
    "TIME_STOP": "持仓时间退出",
    "EXIT_SCAN_INCOMPLETE": "本轮退出扫描未覆盖全部持仓",
}
_COVERAGE_REASONS = {
    "MISSING_CONFIRMED_STOP": "未确认止损覆盖（不等于已确认没有止损）",
    "LOCAL_ORDERS_NOT_FINAL": "本地存在未终结订单",
    "VENUE_LEDGER_POSITION_MISMATCH": "交易所与本地持仓不一致",
    "NONEXCLUSIVE_SYMBOL_OWNERSHIP": "同标的存在多个本地归属",
    "INCOMPLETE_ACCOUNT_READ": "账户信息读取不完整",
    "LEDGER_OVERCLOSED": "台账累计平仓量异常",
    "OPEN_ORDINARY_ORDERS": "存在普通挂单",
    "UNOWNED_ORDINARY_ORDER": "存在未归属普通订单",
    "UNOWNED_CONDITIONAL_ORDER": "存在未归属条件订单",
    "DUPLICATE_ALGO_OWNERSHIP": "条件单归属重复",
    "CONDITIONAL_WITHOUT_LEDGER_EXPOSURE": "条件单缺少本地仓位归属",
    "PROTECTION_TERMS_MISMATCH": "保护单条款不一致",
    "LOCAL_FACTS_CHANGED_DURING_INVENTORY": "核对期间本地状态发生变化",
    "INVALID_COVERAGE_FACTS": "保护覆盖证据无效",
    "EXCLUDED_POSITION_HAS_LOCAL_OWNERSHIP": "排除仓位仍有本地归属",
}


def health_notification_details(code, raw):
    """Strict typed projection: never forward raw stored errors or responses."""
    if code == "JEV_LOCAL_DAILY_BUDGET_EXHAUSTED":
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("status"), str)
            or raw["status"] not in {"CURRENT", "STALE", "UNAVAILABLE"}
        ):
            return {"status": "UNAVAILABLE"}
        if raw["status"] != "CURRENT":
            return {"status": raw["status"]}
        fields = (
            "observed_at_ms",
            "day_start_ms",
            "reset_at_ms",
            "used_tokens",
            "limit_tokens",
            "reservation_tokens",
        )
        if (
            any(
                type(raw.get(key)) is not int or not 0 <= raw[key] <= 2**53 - 1
                for key in fields
            )
            or any(
                raw[key] > 253402214400000
                for key in ("observed_at_ms", "day_start_ms", "reset_at_ms")
            )
            or not 0 < raw["limit_tokens"] <= 1000000
            or not 0 < raw["reservation_tokens"] <= 1000000
            or raw["reset_at_ms"] - raw["day_start_ms"] != 86400000
            or (raw["day_start_ms"] + 28800000) % 86400000
            or not raw["day_start_ms"] <= raw["observed_at_ms"] < raw["reset_at_ms"]
            or not isinstance(raw.get("unavailable_action"), str)
            or raw["unavailable_action"] not in {"BASELINE", "SKIP"}
        ):
            return {"status": "UNAVAILABLE"}
        return {
            "status": "CURRENT",
            **{key: raw[key] for key in fields},
            "unavailable_action": raw["unavailable_action"],
        }
    if code not in {"ORDER_PROGRESS_STALLED", "POSITION_SAFETY_BLOCKED"}:
        return None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("status"), str)
        or raw["status"] not in {"CURRENT", "STALE", "UNAVAILABLE"}
    ):
        return {"status": "UNAVAILABLE"}
    if raw["status"] != "CURRENT":
        return {"status": raw["status"]}
    observed = raw.get("observed_at_ms")
    if type(observed) is not int or not 0 <= observed <= 253402214400000:
        return {"status": "UNAVAILABLE"}
    result = {"status": "CURRENT", "observed_at_ms": observed}
    for group in ("orders", "episodes"):
        records = raw.get(group, [])
        if not isinstance(records, list):
            continue
        clean = []
        for record in records[:20]:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("status"), str)
                or record["status"] not in _HEALTH_STATUSES
            ):
                continue
            symbol = record.get("symbol")
            if not isinstance(symbol, str) or not re.fullmatch(
                r"[A-Z0-9]{1,24}(?:USDT|USDC|USD)", symbol
            ):
                continue
            item = {"symbol": symbol, "status": record["status"]}
            for identity in ("episode_id", "order_id"):
                value = record.get(identity)
                if isinstance(value, str):
                    try:
                        if str(UUID(value)) == value:
                            item[identity] = value
                    except ValueError:
                        pass
            for field, allowed in (
                ("leg", {"OPEN", "CLOSE"}),
                ("source", {"EXIT", "EMERGENCY", "LEDGER"}),
                ("reason", _HEALTH_REASONS),
                ("outcome", _HEALTH_STATUSES),
            ):
                value = record.get(field)
                if isinstance(value, str) and value in allowed:
                    item[field] = value
            idle = record.get("idle_seconds")
            if type(idle) is int and 0 <= idle <= 315360000:
                item["idle_seconds"] = idle
            quantity = record.get("remaining_quantity")
            if isinstance(quantity, str) and re.fullmatch(
                r"[0-9]{1,30}(?:\.[0-9]{1,18})?", quantity
            ):
                item["remaining_quantity"] = quantity
            clean.append(item)
        result[group] = clean[:3]
        if len(clean) > 3 or len(records) > 20:
            result["truncated"] = True
    stages = raw.get("stages", [])
    if isinstance(stages, list):
        result["stages"] = [
            name for name in ("protection", "exits", "settlement") if name in stages
        ]
    codes = raw.get("coverage_codes", [])
    if isinstance(codes, list):
        result["coverage_codes"] = sorted(
            {
                code
                for code in codes[:20]
                if isinstance(code, str) and code in _COVERAGE_REASONS
            }
        )[:6]
        result["unknown_coverage"] = raw.get("unknown_coverage") is True or any(
            not isinstance(code, str) or code not in _COVERAGE_REASONS
            for code in codes[:20]
        )
    if raw.get("truncated") is True:
        result["truncated"] = True
    progress = raw.get("in_progress")
    if type(progress) is int and 0 <= progress <= 1000:
        result["in_progress"] = progress
    reason = raw.get("exit_reason")
    if isinstance(reason, str) and reason in _HEALTH_REASONS:
        result["exit_reason"] = reason
    scan = raw.get("scan")
    if isinstance(scan, dict):
        selected, remaining, complete = (
            scan.get("selected"),
            scan.get("remaining_count_lower_bound"),
            scan.get("complete"),
        )
        if (
            type(selected) is int
            and 0 <= selected <= 20
            and type(remaining) is int
            and 0 <= remaining <= 1000000
            and type(complete) is bool
            and ((complete and remaining == 0) or (not complete and remaining > 0))
        ):
            result["scan"] = {
                "selected": selected,
                "remaining_count_lower_bound": remaining,
                "complete": complete,
            }
    # A pipeline code is not a fresh per-position exchange proof.
    result["protection"] = "UNKNOWN"
    return result


def _health_details_text(code, raw):
    details = health_notification_details(code, raw)
    if details is None:
        return []
    if details["status"] == "STALE":
        return ["详情：最近诊断已过期，不作为当前仓位或保护状态。"]
    if details["status"] != "CURRENT":
        return ["详情：暂时无法读取；本项告警仍有效，请核对交易健康记录。"]
    lines = [f"诊断采样：{_time(details['observed_at_ms'])}"]
    if code == "JEV_LOCAL_DAILY_BUDGET_EXHAUSTED":
        lines.extend(
            [
                f"共享本地输入额度：已计 {details['used_tokens']} / {details['limit_tokens']} tokens（含未确认用量的保守预留）",
                f"下一次需预留：{details['reservation_tokens']} tokens",
                "账户处置：仅新鲜且通过原风控的候选可走 BASELINE；不覆盖模型否决。"
                if details["unavailable_action"] == "BASELINE"
                else "账户处置：跳过需要模型意见的新候选，不追补过期信号。",
                f"新预算窗口：{_time(details['reset_at_ms'])}；届时仍需检查限流与调用结果。",
            ]
        )
        return lines
    if "exit_reason" in details:
        lines.append("退出环节：" + _HEALTH_REASONS[details["exit_reason"]])
    if "scan" in details:
        scan = details["scan"]
        if scan["complete"]:
            lines.append(f"本轮退出扫描已覆盖全部 {scan['selected']} 笔持仓。")
        else:
            lines.append(
                f"本轮已扫描 {scan['selected']} 笔，至少 {scan['remaining_count_lower_bound']} 笔待轮转；扫描未完成不等于平仓失败。"
            )
    for record in details.get("orders", []) + details.get("episodes", []):
        leg = {"OPEN": "开仓", "CLOSE": "平仓"}.get(record.get("leg"), "退出")
        if record.get("source") == "EMERGENCY":
            leg = "应急退出"
        line = f"• {record['symbol']}｜{leg}｜{_HEALTH_STATUSES[record['status']]}"
        if "idle_seconds" in record:
            line += f"｜已持续 {record['idle_seconds']} 秒"
        if "remaining_quantity" in record:
            line += f"｜本地剩余 {record['remaining_quantity']}"
        lines.append(line)
        if "reason" in record:
            lines.append("  原因：" + _HEALTH_REASONS[record["reason"]])
        if "outcome" in record:
            lines.append("  回执：" + _HEALTH_STATUSES[record["outcome"]])
        if "episode_id" in record:
            lines.append("  仓位参考：" + record["episode_id"][-8:])
    if details.get("stages"):
        names = {"protection": "保护", "exits": "退出", "settlement": "结算"}
        lines.append(
            "受影响环节：" + "、".join(names[name] for name in details["stages"])
        )
    lines += [
        "覆盖核对：" + _COVERAGE_REASONS[code]
        for code in details.get("coverage_codes", [])
    ]
    if details.get("unknown_coverage"):
        lines.append("覆盖核对：存在未分类异常，请查看受控诊断。")
    if details.get("in_progress"):
        lines.append(
            f"另有 {details['in_progress']} 项退出已提交处理中，不将其视为平仓失败。"
        )
    lines.append("当前交易所保护状态：未知，需独立核对；不会盲目重复下单。")
    if details.get("truncated"):
        lines.append("仅展示部分异常，完整范围见交易健康记录。")
    return lines


class TelegramOperationalSink:
    def __init__(
        self,
        *,
        token,
        chat_id,
        environment,
        connection_factory=HTTPSConnection,
        alerts_chat_id=None,
    ):
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{15,}", token)
            or not isinstance(chat_id, str)
            or not re.fullmatch(r"-?[1-9][0-9]{0,19}", chat_id)
            or environment not in {"SANDBOX", "LIVE"}
            or not callable(connection_factory)
        ):
            raise ValueError("explicit Telegram credentials and environment required")
        self._token, self._chat_id = token, chat_id
        self.environment, self._connection = environment, connection_factory
        self._alerts = None
        if alerts_chat_id is not None:
            if not isinstance(alerts_chat_id, str) or not re.fullmatch(
                r"[1-9][0-9]{0,19}", alerts_chat_id
            ):
                raise ValueError("positive private Telegram alert destination required")
            self._alerts = TelegramOperationalSink(
                token=token,
                chat_id=alerts_chat_id,
                environment=environment,
                connection_factory=connection_factory,
            )

    @property
    def pin_destination(self):
        return self._token.split(":", 1)[0] + ":" + self._chat_id

    def __call__(self, event_id, scope, kind, payload, *, receipt=None):
        if scope != self.environment:
            return False
        if self._alerts is not None and kind not in {"TRADE_OPENED", "TRADE_CLOSED"}:
            return self._alerts(event_id, scope, kind, payload, receipt=receipt)
        if kind in {"TRADE_OPENED", "TRADE_CLOSED"}:
            text = _trade_text(scope, kind, event_id, payload)
        elif (
            kind in {"MARKET_FAILURE", "MARKET_RECOVERED"}
            and "observed_at_ms" in payload
        ):
            reasons = {
                "QUOTA_DENIED": "本地请求额度不足或限流冷却中",
                "RATE_LIMITED": "交易所接口限流",
                "PUBLIC_HTTP_ERROR": "交易所 HTTP 响应异常",
                "PUBLIC_TRANSPORT_FAILURE": "网络、超时或响应解析异常",
                "PUBLIC_API_ERROR": "交易所响应格式或 API 错误",
                "PUBLIC_RESPONSE_TOO_LARGE": "接口响应超过安全大小限制",
                "PUBLIC_MARKET_DISABLED": "行情接口未启用",
                "PUBLIC_ENDPOINT_DISABLED": "请求端点未启用",
            }
            recovered = kind == "MARKET_RECOVERED"
            text = "\n".join(
                [
                    f"{'✅ 行情采集恢复' if recovered else '⚠️ 行情采集异常'} [V2 {scope}]",
                    "后续行情批次已成功处理；不代表全部交易环节健康。"
                    if recovered
                    else "原因："
                    + reasons.get(payload.get("reason_code"), "未分类采集异常"),
                    "原因码："
                    + str(
                        "RECOVERED"
                        if recovered
                        else payload.get("reason_code")
                        if payload.get("reason_code") in reasons
                        else "PUBLIC_UNKNOWN"
                    ),
                    f"持续时间：{max(0, int(payload['duration_ms'])) // 1000} 秒（截至本次记录）",
                    f"开始：{_time(payload['started_at_ms'])}",
                    f"时间：{_time(payload['observed_at_ms'])}",
                ]
            )
        elif kind == "HOST_RESOURCE_HEALTH":
            descriptions = {
                "HOST_DISK_LOW": "磁盘余量低于预警线，请检查临时测试数据和日志增长。",
                "HOST_DISK_CRITICAL": "磁盘余量低于采集安全门槛；行情采集将暂停，新开仓会因行情过期被阻止。",
                "HOST_MARKET_STALE": "共享行情心跳已过期；请检查采集进程、磁盘与数据依赖。",
                "RETENTION_TIMER_DISABLED": "定时清理任务未启用，请检查维护计时器；不要手动删除交易证据。",
                "RETENTION_JOB_FAILED": "定时清理任务执行失败，请检查维护服务日志；失败批次不会删除交易证据。",
                "RETENTION_JOB_STALE": "定时清理任务长时间没有完成，请检查调度与维护进程。",
            }
            code = payload.get("error_code")
            if code not in descriptions or payload.get("status") not in {
                "UNAVAILABLE",
                "RECOVERED",
            }:
                return False
            numeric = (
                "free_bytes",
                "minimum_free_bytes",
                "warning_free_bytes",
                "observed_at_ms",
            )
            if any(
                type(payload.get(k)) is not int or not 0 <= payload[k] <= 2**63 - 1
                for k in numeric
            ):
                return False
            recovered = payload["status"] == "RECOVERED"
            text = "\n".join(
                [
                    f"{'✅ 主机资源恢复' if recovered else '⚠️ 主机资源告警'} [V2 {scope}]",
                    f"类型：{code}",
                    "本项条件已解除；不代表交易全链路均已恢复。"
                    if recovered
                    else descriptions[code],
                    f"磁盘可用：{payload['free_bytes'] / 1024**3:.2f} GiB",
                    f"采集保护线：{payload['minimum_free_bytes'] / 1024**3:.2f} GiB",
                    f"提前预警线：{payload['warning_free_bytes'] / 1024**3:.2f} GiB",
                    f"时间：{_time(payload['observed_at_ms'])}",
                ]
            )
        elif kind == "TRADING_HEALTH":
            code = payload.get("error_code")
            if code not in BUSINESS_HEALTH_MESSAGES or payload.get("status") not in {
                "UNAVAILABLE",
                "RECOVERED",
            }:
                return False
            recovered = payload["status"] == "RECOVERED"
            text = "\n".join(
                [
                    f"{'✅ 已恢复' if recovered else '🚨 交易链路告警'} [V2 {scope}]",
                    f"账户：{payload['account_id']}",
                    f"类型：{code}",
                    "本项异常条件已解除；不代表所有环节均已恢复。"
                    if recovered
                    else BUSINESS_HEALTH_MESSAGES[code],
                    *(
                        _health_details_text(code, payload["details"])
                        if not recovered and "details" in payload
                        else []
                    ),
                    f"时间：{_time(payload['observed_at_ms'])}",
                ]
            )
        elif kind not in {
            "ACCOUNT_INVENTORY",
            "MARKET_FAILURE",
            "CANDLE_QUARANTINED",
            "PROTECTION_RECOVERY",
            "TRADING_DAEMON",
        }:
            return False
        else:
            # Never forward arbitrary fields, API responses, balances or exception text.
            allowed = (
                "status",
                "blockers",
                "position_count",
                "ordinary_order_count",
                "conditional_order_count",
                "stage",
                "error_code",
                "outcome",
                "state_id",
                "parent_status",
                "account_id",
                "observed_at_ms",
            )
            clean = {key: payload[key] for key in allowed if key in payload}
            text = f"[V2 {scope}] {kind}\nevent_id={event_id}\n" + json.dumps(
                clean, ensure_ascii=False, sort_keys=True
            )
        if len(text) > 3500:
            raise TelegramDeliveryError("NOTIFICATION_TOO_LARGE")
        body = json.dumps({"chat_id": self._chat_id, "text": text}).encode()
        conn = None
        try:
            conn = self._connection("api.telegram.org", timeout=10)
            conn.request(
                "POST",
                "/bot" + self._token + "/sendMessage",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise TelegramDeliveryError("NOTIFICATION_UNCONFIRMED")
            value = json.loads(raw)
            if (
                value.get("ok") is not True
                or type(value["result"]["message_id"]) is not int
                or value["result"]["message_id"] <= 0
                or str(value["result"]["chat"]["id"]) != self._chat_id
            ):
                raise TelegramDeliveryError("NOTIFICATION_UNCONFIRMED")
            if receipt is not None:
                receipt(value["result"]["message_id"])
            return True
        except Exception:  # noqa: BLE001 - suppress credential-bearing URL/response errors
            raise TelegramDeliveryError("NOTIFICATION_UNCONFIRMED") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001, S110 - never replace delivery outcome
                    pass

    def pin(self, message_id):
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("confirmed Telegram message ID required")
        conn = None
        try:
            conn = self._connection("api.telegram.org", timeout=10)
            conn.request(
                "POST",
                "/bot" + self._token + "/pinChatMessage",
                body=json.dumps(
                    {
                        "chat_id": self._chat_id,
                        "message_id": message_id,
                        "disable_notification": True,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise TelegramDeliveryError("PIN_UNCONFIRMED")
            value = json.loads(raw)
            if value.get("ok") is not True or value.get("result") is not True:
                raise TelegramDeliveryError("PIN_UNCONFIRMED")
            return True
        except Exception:  # noqa: BLE001 - never expose token-bearing Telegram errors
            raise TelegramDeliveryError("PIN_UNCONFIRMED") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001, S110 - preserve confirmed result
                    pass
