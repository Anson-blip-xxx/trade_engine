"""Read-only dashboard failure isolation and bounded recent-signal selection."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from services import v2_dashboard as dashboard
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import intent
from v2_core.intents import IntentStore

database = database_fixture


def test_recent_trade_limit_preserves_order_and_excludes_live(database):
    store = IntentStore(database)
    for _ in range(106):
        store.admit(intent())
    for _ in range(4):
        store.admit(replace(intent(), environment="LIVE"))
    with database() as conn:
        expected = [
            str(row[0])
            for row in conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE environment='SANDBOX' "
                "ORDER BY created_at DESC,intent_id DESC LIMIT 100"
            )
        ]
    result = dashboard.DashboardData(database).overview(include_funnel=False)
    assert len(result["trades"]) == 100
    assert [row["id"] for row in result["trades"]] == expected
    assert result["summary"]["trade_intents"] == 106


def test_core_overview_does_not_execute_optional_analytics(database, monkeypatch):
    def forbidden(*args):
        raise AssertionError("core dashboard must not run the daily funnel")

    monkeypatch.setattr(dashboard, "signal_funnel", forbidden)
    report = dashboard.DashboardData(database).overview(include_funnel=False)
    assert report["signal_funnel_status"] == "DEFERRED"
    assert report["summary"]["open_positions"] == 0


def test_real_sql_timeout_rolls_back_savepoint_not_dashboard(database, monkeypatch):
    def slow(conn, account):
        conn.execute("SET LOCAL statement_timeout='1ms'")
        conn.execute("SELECT pg_sleep(0.05)")

    monkeypatch.setattr(dashboard, "signal_funnel", slow)
    data = dashboard.DashboardData(database)
    report = data.overview()
    assert report["signal_funnel_status"] == "TIMEOUT"
    assert report["trades"] == []
    assert report["summary"]["settled_trades"] == 0
    assert data.funnel()["signal_funnel_status"] == "TIMEOUT"


def test_unexpected_errors_are_not_mislabelled_as_empty_funnel(database, monkeypatch):
    def broken(*args):
        raise RuntimeError("broken query")

    monkeypatch.setattr(dashboard, "signal_funnel", broken)
    with pytest.raises(RuntimeError, match="broken query"):
        dashboard.DashboardData(database).funnel()


def test_per_source_top_n_matches_global_latest_and_excludes_other_environments(
    database,
):
    with database() as conn:
        conn.execute("""INSERT INTO v2_inbound_signals
          (signal_id,source,environment,request_key,snapshot,received_at)
          SELECT md5(n::text)::uuid,
            (ARRAY['s3','tv_bridge','quant','unrelated'])[1+n%4],
            CASE WHEN n%7=0 THEN 'LIVE' ELSE 'SANDBOX' END,
            n::text,'{}'::jsonb,statement_timestamp()-n*interval '1 second'
          FROM generate_series(1,200) n""")
        expected = [
            str(r[0])
            for r in conn.execute("""SELECT signal_id
          FROM v2_inbound_signals WHERE environment='SANDBOX'
          AND source IN ('s3','tv_bridge','quant') ORDER BY received_at DESC LIMIT 24""")
        ]
    report = dashboard.DashboardData(database).overview(include_funnel=False)
    assert [r["id"] for r in report["signals"]] == expected
    assert len(expected) == 24


def test_browser_funnel_failure_account_race_and_timeout_recovery():
    script = Path(__file__).resolve().parents[2] / "web/v2_dashboard/app.js"
    result = subprocess.run(
        [
            "node",
            "-e",
            r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={},timers=[];
const get=id=>nodes[id] ||= {addEventListener(){},classList:{remove(){}},setAttribute(){},
  replaceChildren(){},close(){this.open=false;}};
const sandbox={document:{getElementById:get,querySelectorAll:()=>[],addEventListener(){}},
  window:{addEventListener(){}},fetch:()=>new Promise(()=>{}),setInterval(){},AbortController,
  setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const run=code=>vm.runInContext(code,sandbox);
(async()=>{
  for (const phase of ['BLOCKED','STARTING','STOPPED']) {
    run(`renderMetrics({execution_account:{alias:'Account A',phase:'${phase}',blockers:['WORKER_START_FAILED']},summary:{realized_pnl:'42'}})`);
    assert.doesNotMatch(nodes['execution-account'].textContent,/执行已确认/);
    assert.match(nodes['execution-account'].textContent,/WORKER_START_FAILED/);
  }
  run(`state.data={execution_account:{registry_id:'a',epoch:1},summary:{realized_pnl:'42'}}`);
  sandbox.fetch=async()=>{throw new Error('offline');};
  await run('refreshFunnel(state.data)');
  assert.equal(run('state.data.summary.realized_pnl'),'42');
  assert.match(nodes['signal-funnel'].innerHTML,/暂不可用/);
  assert.doesNotMatch(nodes['signal-funnel'].innerHTML,/尚未收到/);
  sandbox.fetch=async()=>({ok:true,json:async()=>({execution_account:{registry_id:'b',epoch:2},signal_funnel:[]})});
  await run('refreshFunnel(state.data)');
  assert.match(nodes['signal-funnel'].innerHTML,/暂不可用/);
  let resolve;
  sandbox.fetch=()=>new Promise(r=>resolve=r);
  const pending=run('refreshFunnel(state.data)');
  run(`state.data={execution_account:{registry_id:'b',epoch:2}}`);
  nodes['signal-funnel'].innerHTML='new account';
  resolve({ok:true,json:async()=>({execution_account:{registry_id:'a',epoch:1},signal_funnel:[]})});
  await pending;assert.equal(nodes['signal-funnel'].innerHTML,'new account');
  sandbox.fetch=async()=>({ok:true,json:async()=>({execution_account:{registry_id:'b',epoch:2},signal_funnel:[{source:'s3',received:3,processed:2,intents:1,filled:1}]})});
  await run('refreshFunnel(state.data)');assert.match(nodes['signal-funnel'].innerHTML,/收到 3/);
  sandbox.fetch=(url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('timeout'))));
  run('state.refreshing=false');const refresh=run('refresh()');
  timers.at(-1)();await refresh;
  assert.equal(run('state.refreshing'),false);assert.equal(nodes.refresh.disabled,false);
  assert.equal(run('state.data'),null);assert.equal(nodes.error.hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
""",
            str(script),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
