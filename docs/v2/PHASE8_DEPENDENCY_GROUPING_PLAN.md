# Phase 8-05A — Service Constructor Dependency Grouping Plan（docs/tests only；production 0 diff）

> @ `eda8868`。C3 设计阶段：**先冻结再分组**。核心命题：能否在不破坏
> Phase 7 最重要 late-binding 的前提下缩短 constructor。若不能证明
> patch-after-import / patch-before-factory / runtime backing identity 仍成立，
> 不进入 P8-05B。

## 一、Constructor Inventory（AST 提取，@ HEAD）

| Service | args | 注入位 |
|---|---|---|
| ExecutionService | 1 | `binance` |
| ProtectionService | 5 | enqueue_fn/start_worker_fn/place_algo_sl_fn/cancel_id_fn/cancel_all_fn |
| PositionStateService | 5 | state_port/marker_set/marker_get/marker_delete/sandbox_check |
| LifecycleService | 21 | log,now,load,save,wcr,mc,clr,s6,sandbox,wkr,enq,acx,cxa,pg,rq,posid,exec_fn,close_fn,ci,pi,lce |
| ReconcileService | 22 | lgt,now,load,save,sandbox,exf,gpx,lacq,lrel,wcr,mc,s6,posid,rdget,rdset,rqst,tgt,tgc,pg,sk,pid,uid |
| MonitoringService | **40** | log,now,load,save,m1,gcl,gq,summ,s6,ghb,shb,cfg,fund,cls,dc,elm,stag,g1h,us,pc,rq,pp,cts,pt,wsl,wsp,swlu,wst,wcr,trgt,mc,lkey,lttl,inst,lkfn,wsf,oofn,oe,oc,ldr |

## 二、Dependency Domain Classification（责任域，非机械分组）

| Domain | 名 | 内容 |
|---|---|---|
| A | Runtime/Clock | log, now, pid, uid |
| B | State IO | load, save, wcr, mc, clr, posid, marker( rdset/rdget), s6 密读 |
| C | Exchange/Market IO | exf, gpx, s6, dc, rqst, fund, sandbox |
| D | Execution/Lifecycle | exec_fn, ci, pi |
| E | Ledger/Persistence | pg, rq, close_fn, lce |
| F | Action/Exit (Returning) | cts, pt, pp, us, pc, elm, stag |
| G | Ws/Leader Runtime | wsl, wsp, swlu, wst, lkey, lttl, inst, lkfn, wsf, oofn, oe, oc, ldr |
| H | Coordination/Locking | lacq, lrel |
| I | Queue | enq, wkr, acx, cxa |

## 三、Late-Binding Contract（现有 Golden 实证，勿破坏）

**当前语义（严格保留）**：
1. **fresh instance**：每次 factory 调用返回**新**实例（无 cache）——6/6。
2. **patch-before-factory**：patch pm attr → factory 当场 capture patch 值。
3. **patch-between-factory-calls**：svc1 保持旧绑定，svc2（新 construction）capture 新值 —— **禁止**未来 sprint singleton / cache deps 破坏。

## 四、Monkeypatch Matrix（现状汇总）

| Service | Seam | Patch Target | 现行为 |
|---|---|---|---|
| Monitoring | fund | `pm._get_funding_rate` | liquidation/warning 直接切换（frozen） |
| | cls | `pm._close` | 5-tuple / None 决策级 |
| | load/save | `pm._load/_save` | 三层链替换 |
| | ghost | `pm._ghost_cleanup/_TRY_RGT` | Step0 spy |
| | us/pc/cts/pt/dc/elm/stag/g1h/pp/rq | inline helpers | 各步 break/exit 决策 |
| Lifecycle | exec_fn | `pm._execution_service` | intent 执行 |
| | ci/pi | `pm._exec_core.*` | intent 构造 |
| | mc/clr/wcr | marker | close 语义 |
| | cxa/acx/enq/wkr | `pm._algo_*` | Protection |
| | pg/rec | s6[7] / `_pg_record_event` | ledger |
| | lce | `pm._log_close_error`（60s 节流） | 失败日志 |
| Reconcile | lacq/lrel | redis lock | 分布式锁 |
| | pg/tgt/tgc | PG/TG notify | PMB-26 不对称 |
| | rdget/rdset | alert pending/seen | 30s/24h 冻结 |
| | exf/gpx | light | exchange compare |
| Ledger | income/ch/analysis/tg | `tr.record_trade` 注入 | P7-03A 冻结 |
| State | rdget/rdset | PM `_rget/_rset` | marker |
| Protection | worker/place/cancel | pm._algo_* | queue/worker |

## 五、Runtime Identity Constraints（Bundle 后必须仍然成立）

`_WS_POSITIONS` / `_WS_LOCK` / `_RECENTLY_GHOSTED` / `_ALGO_QUEUE` /
worker flag —— 若入 bundle：仍引用同一 backing（identity 断言已有），禁止 snapshot/copy。
frozen dataclass（未来 P8-05B）仅冻结 attributes reassignment，不复制 underlying object。

## 六、Proposed Bundles（未来 P8-05B）

- **LifecycleService** → `LifecycleDeps(runtime: (log, now), execution: (exec_fn, ci, pi), state: (load, save, wcr, mc, clr, posid, rq), protection: (wkr, enq, acx, cxa), action: (close_fn, s6, sandbox, pg, lce))` — **5 bundles**
- **ReconcileService** → `runtime(lgt, now, pid, uid) / state(load, save, wcr, mc, posid) / exchange(exf, gpx, s6, rqst, sandbox) / lock(lacq, lrel) / notify(pg, tgt, tgc) / alert(rdset, rdget) / migration(sk)` — **5 bundles**（notify+alert→notification；state+alert 合并）
- **MonitoringService** → `runtime(log, now)/state(load, save, wcr, mc, gq, ghb, shb, summ, m1)/market(dc, s6, cfg, fund, elm, stag, g1h)/action(cls, us, pc, rq, cts, pt, pp)/ws_runtime(wsl, wsp, swlu, wst, lkey, lttl, inst, ldr, lkfn, wsf, oofn, oe, oc, trgt, gcl)` — **5 bundles**

## 七、NO-CHANGE RECOMMENDED

ExecutionService(1)、ProtectionService(5)、PositionStateService(5) —— 参数已极少，收益不显著 → 不改。

## 八、P8-05B Migration Order（建议，不实施）

1. **LifecycleService**（seam 顺）→ 2. **ReconcileService** → 3. **MonitoringService**（参数最多、WS/leader 最敏感，最后）→ (LEDGER/STATE/PROTECTION 不改)。

**Rollback**：一 service 一 commit；任一 Golden fail 即单 service revert。

## 九、P8-05B Constraints

- bundle = dataclass(data-only holder)；无 business method/IO/locator/
  `__getattr__`/global registry
- bundle 实例 **由 factory 调用时创建**（无 `DEFAULT_..._DEPS = ...`）
- frozen dataclass（若包含 runtime backing）仅冻结 reassignment，
  **不**快照 underlying mutable（文档标注）
- 语义分 domain，大小定制，不 hard-cycle 照搬 3~5

## 十、P8-05A Exit：17 criteria 全绿（测试 44+24 全绿）→ P8-05A PASS
