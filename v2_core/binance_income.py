"""Read-only, bounded income import. FETCHED means pages fetched, not settlement.

No high-water timestamp is used: replay overlapping windows to detect late data.
Each page and its progress commit together. A killed process leaves RUNNING;
restart a new run from page 1, deduplicating previously persisted facts.
"""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import uuid4

from v2_core.binance import identifier
from v2_core.evidence import canonical
from v2_core.income import IncomeJournal, IncomeScope
from v2_core.ledger import amount
from v2_core.state import normalized

_DAY = 86400000


class BinanceIncomeImporter:
    def __init__(
        self,
        connection_factory,
        request,
        *,
        account_id,
        environment,
        clock_ms,
        max_pages=20,
    ):
        if not callable(request) or not callable(clock_ms):
            raise TypeError("explicit read transport and clock required")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("bounded income pagination required")
        if (
            getattr(request, "account_id", account_id) != account_id
            or getattr(request, "environment", environment) != environment
        ):
            raise ValueError("income transport binding mismatch")
        self.scope = IncomeScope("BINANCE", account_id, environment, "FUTURES")
        self._connect, self.request, self.clock_ms, self.max_pages = (
            connection_factory,
            request,
            clock_ms,
            max_pages,
        )

    @staticmethod
    def _normalize(row, start_ms, end_ms):
        if not isinstance(row, dict):
            raise TypeError("income row must be an object")
        source_id = identifier(row["tranId"])
        for key in ("incomeType", "asset"):
            normalized(row[key])
        symbol = row["symbol"]
        if not isinstance(symbol, str):
            raise TypeError("income symbol must be text")
        if symbol:
            normalized(symbol)
        timestamp = row["time"]
        if type(timestamp) is not int or not start_ms <= timestamp <= end_ms:
            raise ValueError("income timestamp outside requested window")
        amount(row["income"])
        trade_id = row.get("tradeId", "")
        if not isinstance(trade_id, str) or (
            trade_id and not (trade_id.isascii() and trade_id.isdecimal())
        ):
            raise ValueError("invalid referenced trade identity")
        return {
            "income_type": row["incomeType"],
            "source_id": source_id,
            "symbol": symbol,
            "amount_text": row["income"],
            "currency": row["asset"],
            "occurred_at_ms": timestamp,
            "evidence": {"source": "binance-income", "trade_id": trade_id},
        }

    def import_window(self, *, start_ms, end_ms):
        now = self.clock_ms()
        if (
            any(type(v) is not int for v in (start_ms, end_ms, now))
            or not 0 <= start_ms <= end_ms <= now
        ):
            raise ValueError("explicit past income window required")
        # Conservative policy within the venue's three-month retention period.
        if end_ms - start_ms > 7 * _DAY or start_ms < now - 80 * _DAY:
            raise ValueError("income window exceeds bounded import/retention policy")
        run_id = str(uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO v2_income_imports(run_id,scope,start_ms,end_ms,status) VALUES (%s,%s::jsonb,%s,%s,'RUNNING')",
                (run_id, canonical(asdict(self.scope)), start_ms, end_ms),
            )
        seen, rows = set(), 0
        status, error = "PARTIAL", None
        try:
            for page_number in range(1, self.max_pages + 1):
                page = self.request(
                    "GET",
                    "/fapi/v1/income",
                    {
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "page": page_number,
                        "limit": 1000,
                    },
                )
                if not isinstance(page, list) or len(page) > 1000:
                    raise ValueError("invalid income page")
                facts = [self._normalize(row, start_ms, end_ms) for row in page]
                identities = {(f["income_type"], f["source_id"]) for f in facts}
                unstable = len(identities) != len(facts) or bool(identities & seen)
                with self._connect() as conn:
                    journal = IncomeJournal(lambda: nullcontext(conn))
                    for fact in facts:
                        journal.ingest(self.scope, **fact)
                    conn.execute(
                        "UPDATE v2_income_imports SET pages=%s,row_count=%s,updated_at=clock_timestamp() WHERE run_id=%s",
                        (page_number, rows + len(facts), run_id),
                    )
                rows += len(facts)
                seen.update(identities)
                if unstable:
                    break  # Page shifts/duplicates cannot establish coverage.
                if len(page) < 1000:
                    status = "FETCHED"
                    break
        except Exception as exc:  # noqa: BLE001 - preserve earlier pages, expose safe error category
            status, error = "FAILED", type(exc).__name__
        with self._connect() as conn:
            progress = conn.execute(
                "UPDATE v2_income_imports SET status=%s,error_code=%s,updated_at=clock_timestamp() WHERE run_id=%s RETURNING pages,row_count",
                (status, error, run_id),
            ).fetchone()
        return {
            "run_id": run_id,
            "status": status,
            "pages": progress[0],
            "rows": progress[1],
            "error_code": error,
        }
