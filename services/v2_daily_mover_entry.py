"""Independent scheduled research. No account credentials or order authority."""

import argparse
import os
import time
from contextlib import contextmanager
from decimal import Decimal

from services.v2_daily_mover_flow import DailyMoverFlowReview
from services.v2_daily_mover_job import DailyMoverJob, emit
from services.v2_daily_mover_market import MoverMarket, MoverMarketError
from services.v2_daily_mover_notify import make_notifier
from services.v2_daily_mover_store import DailyMoverStore
from services.v2_mover_flow_evaluation import FlowHistoryEvaluation
from services.v2_mover_flow_market import FlowMarket
from services.v2_mover_flow_store import FlowEvidenceStore
from services.v2_reentry_archive import ReentryArchive
from v2_core.daily_movers import previous_day_window
from v2_core.database import connection_factory
from v2_core.shared_market import HostBudget


def archive_client(**kwargs):
    import clickhouse_connect

    # readonly=1 rejects even stricter SET overrides; verify fixed server caps.
    kwargs.pop("settings", None)
    client = clickhouse_connect.get_client(username="tradev2shadow", **kwargs)
    try:
        rows = client.query(
            "SELECT getSetting('readonly'),getSetting('max_threads'),getSetting('max_execution_time'),getSetting('max_result_rows'),getSetting('max_memory_usage')"
        ).result_rows
        if (
            len(rows) != 1
            or len(rows[0]) != 5
            or any(type(v) not in (str, int, float, Decimal) for v in rows[0])
        ):
            raise ValueError()
        ro, threads, seconds, count, memory = (Decimal(str(v)) for v in rows[0])
        if not (
            ro == 1
            and threads == 1
            and 0 < seconds <= 3
            and 0 < count <= 20000
            and 0 < memory <= 134217728
        ):
            raise ValueError()
    except Exception:  # noqa: BLE001 - no raw server/connection diagnostics
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110 - retain fixed verification failure
            pass
        raise ValueError("MOVER_ARCHIVE_PROFILE_UNVERIFIED") from None
    return client


def run(values=None, *, day=None, force_evidence_refresh=False):
    values = os.environ if values is None else values
    base = connection_factory(values["V2_DAILY_MOVER_DSN"], schema="trade_v2")

    @contextmanager
    def connect():
        with base() as c:
            c.execute("SET LOCAL statement_timeout='3s'")
            c.execute("SET LOCAL lock_timeout='100ms'")
            yield c

    store = DailyMoverStore(connect)
    notifier = make_notifier(values)
    # Research-only session lock; never touches a trading/execution lock.
    with connect() as lock:
        acquired = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('daily-mover-review-worker-v1',0))"
        ).fetchone()[0]
        lock.commit()
        if not acquired:
            emit(component="daily-mover-review", status="ALREADY_RUNNING")
            return 0
        archive = None
        try:
            archive = ReentryArchive(factory=archive_client)
            flow_options = {}
            if values.get("V2_MOVER_FLOW_ENABLED", "0") == "1":
                flow_options["flow_reviewer"] = DailyMoverFlowReview(
                    FlowEvidenceStore(connect),
                    lambda: FlowMarket(
                        HostBudget(connect, environment="LIVE", lane="market")
                    ),
                )
                flow_options["flow_evaluator"] = FlowHistoryEvaluation(connect)
            elif values.get("V2_MOVER_FLOW_ENABLED", "0") != "0":
                emit(
                    component="daily-mover-flow",
                    status="DISABLED_INVALID_CONFIG",
                    trading_affected=False,
                )
            job = DailyMoverJob(
                store,
                MoverMarket(HostBudget(connect, environment="LIVE", lane="market")),
                notifier=notifier,
                path_reader=archive.read,
                force_evidence_refresh=force_evidence_refresh,
                **flow_options,
            )
            emit(
                component="daily-mover-review", status="STARTED", orders_submitted=False
            )
            result = job.run(day=day)
            emit(component="daily-mover-review", status="FINISHED", results=result)
            return 0
        except Exception as exc:  # noqa: BLE001 - sanitize research service boundary
            code = (
                str(exc)
                if isinstance(exc, MoverMarketError)
                and str(exc)
                in {
                    "MOVER_RATE_LIMITED",
                    "MOVER_SHARED_QUOTA_WAIT",
                    "MOVER_JOB_DEADLINE",
                    "MOVER_PUBLIC_TRANSPORT_FAILURE",
                    "MOVER_CATALOG_INVALID",
                    "MOVER_CATALOG_BOUND",
                }
                else "MOVER_RESEARCH_JOB_FAILED"
            )
            emit(
                component="daily-mover-review",
                status="DEFERRED",
                error_code=code,
                retry="NEXT_HOURLY_TIMER",
                trading_affected=False,
            )
            # Once-per-day operational DM, separate from account research DMs.
            try:
                target = day or previous_day_window(time.time_ns() // 1_000_000)["date"]
                identity = store.save_failure(target, code)
                if notifier is not None and store.claim_delivery(identity):
                    try:
                        receipt = notifier(
                            f"每日大行情复盘暂未完成 · {target} UTC+8\n原因：{code}\n"
                            "已保存采集进度，下个整点后的20分钟重试。本研究任务不阻塞交易；尚未完成的结果不能当成完整复盘。"
                        )
                    except Exception:  # noqa: BLE001 - ambiguous DM must not be retried
                        receipt = None
                    store.delivered(
                        identity,
                        receipt if type(receipt) is int and receipt > 0 else None,
                    )
            except Exception:  # noqa: BLE001 - failure reporting cannot affect execution
                emit(component="daily-mover-review", status="FAILURE_DM_UNCONFIRMED")
            return 1
        finally:
            try:
                if archive is not None:
                    archive.close()
            except Exception:  # noqa: BLE001 - still release independent research lock
                emit(component="daily-mover-review", status="ARCHIVE_CLOSE_UNCONFIRMED")
            try:
                lock.execute(
                    "SELECT pg_advisory_unlock(hashtextextended('daily-mover-review-worker-v1',0))"
                )
            except Exception:  # noqa: BLE001 - session close releases lock even on DB error
                emit(
                    component="daily-mover-review", status="LOCK_RELEASE_SESSION_CLOSE"
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--day", help="Completed UTC+8 date, at most3days behind yesterday"
    )
    parser.add_argument(
        "--refresh-evidence",
        action="store_true",
        help="Append a fresh research revision without repeating the daily DM",
    )
    args = parser.parse_args()
    raise SystemExit(run(day=args.day, force_evidence_refresh=args.refresh_evidence))


if __name__ == "__main__":
    main()
