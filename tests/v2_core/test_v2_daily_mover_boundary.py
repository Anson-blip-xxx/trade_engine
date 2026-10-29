"""Static deployment boundaries supplement, not replace, least-privilege grants."""

import ast
import re
from pathlib import Path

import pytest

from v2_core.daily_movers import rank_daily_movers

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "v2_core/daily_movers.py",
    "services/v2_daily_mover_market.py",
    "services/v2_daily_mover_store.py",
    "services/v2_daily_mover_job.py",
    "services/v2_daily_mover_evidence.py",
    "services/v2_daily_mover_report.py",
    "services/v2_daily_mover_trade_paths.py",
    "services/v2_daily_mover_entry.py",
    "services/v2_daily_mover_notify.py",
    "services/v2_daily_mover_flow.py",
    "services/v2_mover_flow_market.py",
    "services/v2_mover_flow_store.py",
    "v2_core/mover_flow.py",
    "v2_core/mover_flow_evaluation.py",
    "services/v2_mover_flow_evaluation.py",
)
TRADING_PORTS = {
    "services.v2_execution_controller",
    "services.v2_execution_supervisor",
    "services.v2_testnet_daemon",
    "services.v2_testnet_client",
    "services.v2_binance_executor",
    "v2_core.execution_routes",
    "v2_core.binance",
    "v2_core.transport",
    "v2_core.orders",
    "v2_core.order_executor",
    "v2_core.credential_vault",
}
WRITES = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE\s+(?:TABLE\s+)?|ALTER\s+TABLE|DROP\s+TABLE)\s+(?:[a-z0-9_]+\.)?(v2_[a-z0-9_]+)",
    re.IGNORECASE,
)


def tree(relative):
    return ast.parse((ROOT / relative).read_text())


@pytest.mark.parametrize("relative", FILES)
def test_research_modules_do_not_import_execution_or_credential_vault_ports(relative):
    imports = set()
    for node in ast.walk(tree(relative)):
        if isinstance(node, ast.ImportFrom):
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    assert not imports & TRADING_PORTS


@pytest.mark.parametrize("relative", FILES)
def test_research_literal_sql_never_mutates_financial_or_execution_tables(relative):
    for node in ast.walk(tree(relative)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for table in WRITES.findall(node.value):
                assert (
                    table.lower().startswith("v2_daily_mover_")
                    or table.lower() == "v2_mover_flow_evidence"
                ), (relative, table)


@pytest.mark.parametrize("relative", FILES)
def test_research_does_not_access_exchange_secrets_or_local_business_storage(relative):
    text = (ROOT / relative).read_text()
    for forbidden in (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "X-MBX-APIKEY",
        "v2_credential_vault",
        "SystemdMasterKeys",
        "MASTER_KEY",
        ".write_text(",
        ".write_bytes(",
    ):
        assert forbidden not in text, (relative, forbidden)
    for node in ast.walk(tree(relative)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                "submit_order",
                "create_order",
                "place_order",
                "cancel_order",
                "cancel_all_orders",
                "close_position",
                "change_leverage",
            }


def test_market_transport_is_get_only_public_fixed_host_without_redirect_support():
    module = tree("services/v2_daily_mover_market.py")
    request_calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "request"
    ]
    assert len(request_calls) == 1
    assert isinstance(request_calls[0].args[0], ast.Constant)
    assert request_calls[0].args[0].value == "GET"
    hosts = [
        node.args[0].value
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "factory"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert hosts == ["fapi.binance.com"]
    text = (ROOT / "services/v2_daily_mover_market.py").read_text()
    assert '"/fapi/v1/order"' not in text
    assert '"/fapi/v3/account"' not in text
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            assert not any(alias.name == "requests" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in {"requests", "urllib.request"}


def test_database_migration_only_creates_new_research_tables_and_indexes():
    sql = (ROOT / "db/migrations/20261026_daily_movers.sql").read_text()
    sql = re.sub(r"--[^\n]*", "", sql)
    tables = re.findall(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", sql, re.IGNORECASE
    )
    assert set(tables) == {
        "v2_daily_mover_days",
        "v2_daily_mover_symbols",
        "v2_daily_mover_reports",
        "v2_daily_mover_delivery",
    }
    for table in WRITES.findall(sql):
        assert table.startswith("v2_daily_mover_"), table
    for match in re.finditer(
        r"\b(?:GRANT|TRUNCATE|SECURITY\s+DEFINER|DROP\s+TABLE)\b", sql, re.IGNORECASE
    ):
        pytest.fail(f"Unexpected authority or destructive DDL: {match.group(0)}")
    for name in re.findall(r"DROP\s+TRIGGER\s+IF\s+EXISTS\s+(\w+)", sql, re.IGNORECASE):
        assert name.startswith("v2_daily_mover_")
    assert "v2_orders" not in sql and "v2_trade_intents" not in sql
    assert "v2_execution_routes" not in sql and "v2_credential_vault" not in sql


def test_public_ranking_and_actual_paths_have_distinct_environment_sources():
    job = tree("services/v2_daily_mover_job.py")
    paths = tree("services/v2_daily_mover_trade_paths.py")

    def source(module):
        return next(
            node.value.value
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SOURCE"
                for target in node.targets
            )
        )

    assert source(job) == "BINANCE_USDM_LIVE"
    assert source(paths) == "BINANCE_USDM_SANDBOX"


def test_current_catalog_never_claims_historical_full_market_universe():
    start, end = 1792857600000, 1792944000000
    result = rank_daily_movers(
        [],
        [],
        start_ms=start,
        end_ms=end,
        as_of_ms=end + 1,
        universe_observed_at_ms=end + 1,
    )
    assert result["status"] == "PARTIAL_COVERAGE"
    assert result["universe_basis"] == "CURRENT_DISCOVERY_SURVIVORSHIP_LIMITED"
    assert result["ranking_use"] == "EX_POST_RESEARCH_SELECTION_NOT_PREDICTION"


def test_dedicated_pg_grants_have_no_credential_or_trading_write_authority():
    sql = (ROOT / "deploy/v2-testnet/daily-movers-grants.sql").read_text()
    sql = re.sub(r"--[^\n]*", "", sql)
    assert "v2_credential_vault" not in sql
    assert "EXECUTE" not in sql and "ALL PRIVILEGES" not in sql
    for statement in sql.split(";"):
        if not re.search(r"\b(?:INSERT|UPDATE|DELETE)\b", statement):
            continue
        tables = re.findall(r"trade_v2\.(v2_[a-z_]+)", statement)
        assert tables
        assert all(
            table.startswith("v2_daily_mover_")
            or table in {"v2_egress_budgets", "v2_public_market_budgets"}
            for table in tables
        )
    assert "GRANT SELECT,INSERT ON trade_v2.v2_daily_mover_reports" in sql


def test_protected_separate_process_never_receives_binance_or_master_credentials():
    text = (ROOT / "deploy/v2-testnet/trade-v2-daily-movers.service").read_text()
    lines = text.splitlines()
    assert "User=tradev2movers" in lines and "Group=tradev2movers" in lines
    assert "Type=oneshot" in lines
    assert "NoNewPrivileges=yes" in lines and "ProtectSystem=strict" in lines
    assert "ProtectHome=yes" in lines and "UMask=0077" in lines
    assert "CapabilityBoundingSet=" in lines
    assert [
        line.split(":", 1)[0] for line in lines if line.startswith("LoadCredential")
    ] == ["LoadCredentialEncrypted=v2-notifications"]
    assert not re.search(
        r"(?i)(binance\.env|master.?key|execution-controller\.env)", text
    )
    assert "Restart=always" not in text
    timer = (ROOT / "deploy/v2-testnet/trade-v2-daily-movers.timer").read_text()
    assert "Asia/Shanghai" in timer and "Persistent=true" in timer
