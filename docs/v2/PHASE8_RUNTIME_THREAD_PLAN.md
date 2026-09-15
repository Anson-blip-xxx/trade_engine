# Phase 8-06A — Runtime / Thread Ownership Plan（P8-06A docs/tests only；production 0 diff）

> C7 design freeze：把 thread/runtime glue 迁 `runtime.py` 之前，
> 完整盘点 owner/identity/ Semantics。P8-06B 的所有 migration 必须
> 满足 single backing + legacy alias + import-time worker 起点不变。

## 一、Runtime Inventory（AST 提取 @ cfd9824..HEAD）

| Global | Owner | Type | mutable? | identity-sensitive? | producer | consumer | restart | monkeypatch | C7 candidate? |
|---|---|---|---|---|---|---|---|---|---|
| `_ALGO_QUEUE` | PM | list | Y | Y | `pm._algo_enqueue` / se via import `_algo_enqueue` | worker (pop(0) 11s loop) | 重启丢失（内存） | YES（queue/worker tests） | Thread glue getCandidate（F/A） |
| `_ALGO_QUEUE_LOCK` | PM | threading.Lock | Y（对象自身） | Y | enqueue | worker | 新实例 | — | same |
| `_ALGO_WORKER_STARTED` | PM | bool | Y | Y | `_algo_start_worker`（单 flag 双防） | `_algo_start_worker` | reset False | YES（tests setattr False） | same |
| `_WS_POSITIONS` | PM | dict | Y | Y | `_ws_on_message` (WS leader) | `svc.ws.wsp`、`ws_snapshot` | reset | Y（direct assign） | backing 保留 PM |
| `_WS_LOCK` | PM | Lock | Y | Y | enqueue | on_message / ws_snapshot | 新实例 | — | same |
| `_WS_LAST_UPDATE` | PM | float | Y | Y | `_ws_on_message`（每轮 UPDATE 后联通——`swlu` setter） | `_load` 三层链（via `ws_snapshot`） | reset 0 | — | 同 |
| `_WS_THREAD` | PM | Thread 对象 / None | Y | Y（单 thread） | PM import 沙盒 guard | thread loop | 重启重启后 RB | — | Thread glue 候选 (C/F) |
| `_WS_INSTANCE` | PM | str（`pid-hex8`） | — | X（per-process） | module init | leader lease | restart → 新 id | — | static 句柄 (config level) |
| `_monitor_heartbeat_ts` | PM | float（via dict key） | Y | Y | heartbeat setter `shb(v)` | `ghb()` | reset 0 | YES | backing PM 保留 |
| `_RECENTLY_GHOSTED` | PM | list（dead / no producer） | Y | Y | （无—— producer dead） | monitor_all 消费 | 重启清空（process local） | YES | 监控 ownership 保留 |
| `_POS_CACHE` | SE | dict | Y | Y | `se._update_pos_cache` | `pm._load`；SE 主循环 | reset | YES | **KEEP**（dual-writer + behavior-sensitive） |
| `_CLOSE_ERROR_LOG_TS` | PM | dict | Y | Y | `_log_close_error` | regen export per-symbol | 重启丢失 | YES（throttle） | KEEP |
| `_last_algo_update/_last_api_call` | PM | dict | Y | Y | update | update | reset | YES | KEEP |

## 二、Owner Matrix / Thread Glue 分类

Thread glue（C7 候选迁 runtime.py）：
- `_algo_worker_loop` / `t = threading.Thread(target=_algo_worker_loop, daemon=True, name='algo-worker')`
- `if os.environ.get('PM_NO_WS') != '1': _WS_THREAD = threading.Thread(target=_ws_connect_loop, daemon=True)`
Thread glue A/D（keep-in-place）：
- flag + backing 中有 identity/monkeypatch-facing behavior

Runtime backing（C）：
- queue/flag/lock/backing —— **不迁 backing**（identity 保持 PM）
RuntimeStub/H：heartbeat setter/getter、alert pending/seen key owner
（PM single backing）—— 继续由 factory beacon 作为 callable seam 保留

## 三、Algo Worker Startup（frozen）

```python
def _algo_start_worker():
    global _ALGO_WORKER_STARTED
    if _ALGO_WORKER_STARTED:
        return                      # double start no-op
    _ALGO_WORKER_STARTED = True
    t = threading.Thread(target=_algo_worker_loop, daemon=True, name='algo-worker')
    t.start()
    _pmlog('[AlgoWorker] 后台队列消费线程已启动')
```
- daemon=True / name='algo-worker' / target `_algo_worker_loop` identity 保留
- double-start no-op（`_ALGO_WORKER_STARTED` guard）
- 11s sleep / 1s idle —— inherent worker semantics（禁改）
- **import side effect**：`strategies/shared_executor.py:357 _algo_start_worker()` —— N1 冻结保持

## 四、WS Thread Startup（frozen）

```python
if os.environ.get('PM_NO_WS') != '1':
    _WS_THREAD = threading.Thread(target=_ws_connect_loop, daemon=True)
    _WS_THREAD.start()
```
- 无 name field（与 Algo worker 不同；非 generic runner）
- daemon=True；target=`_ws_connect_loop`（通过 P8-05B3 monitoring ws bundle）
- start at pm module import time —— 不（否则 PM_NO_WS disables）
- 重复 import 仅创建1次；repeated import→ import-time guard

## 五、Queue Identity

```
svc_state = pm._protection_service()
svc_state.enqueue_fn == pm._algo_enqueue → backing `_ALGO_QUEUE`（identity）
monitoring svc.state.gq is pm._RECENTLY_GHOSTED
lifecycle svc.protection.enq is pm._algo_enqueue
```

## 六、WS Identity

`mon_svc.ws.wsp is pm._WS_POSITIONS` / `wsl is _WS_LOCK`（P8-05B3 frozen）。

## 七、Import Side-effect Matrix

| import | spawn | Redis | HTTP | sleep |
|---|---|---|---|---|
| position_state/ledger/protection/reconcile/lifecycle（services+deps） | ✗ | ✗ | ✗ | ✗ |
| position_monitoring server/deps | ✗ | ✗ | ✗ | ✗ |
| shared.position_manager | **WS thread**（PM_NO_WS=1 disables）；algo worker NOT auto-started | 惰性 | 惰性 | — |
| strategies.shared_executor | algo worker（N1 frozen START） | 惰性 | 惰性 | ✗ |

## 八、Restart Matrix

| State | 重启时 |
|---|---|
| `_ALGO_QUEUE`/`_ALGO_QUEUE_LOCK`/`_ALGO_WORKER_STARTED` | in-memory 丢失（fresh 空 list / False flag）|
| `_WS_POSITIONS/_WS_LAST_UPDATE` | in-memory 丢失，0 → 走 REST/meta 兜底 |
| `_monitor_heartbeat_ts` | 重启 0 |
| `_RECENTLY_GHOSTED` | 重启清空（process local） |
| `_WS_INSTANCE` | 重新生成（pid-hex8） |
| Redis（closed marker / leader lease / alert pending）/ CH / PG | 外部持久存在 |

## 九、Proposed runtime.py scope（P8-06B，GREEN）

**迁**（真 thread glue）：
- algo worker loop + scheduler（11s/1s sleep，threading.Thread start）
- WS thread boot glue（PM_NO_WS guard）
- 常量 `name='algo-worker'` / `daemon=True` / start flag

**不迁 backing**（Identity / monkeypatch seam 保留 PM）：
- queue/flag/heartbeat/_CLOSE_ERROR_LOG_TS/_RECENTLY_GHOSTED/_POS_CACHE → PM original backing
- Singleton flags/qlock紧贴 worker —— `runtime.py` 操作 PM backing via injectable ref seam
- legacy PM symbol → alias（runtime.py 函数作为 facade 调用）

** KEEP/DEFERRED**：_POS_CACHE（dual-writer cross-module）、_CLOSE_ERROR_LOG_TS、
`_last_*`、`_WS_THREAD`（PM start point 保留）。

## 十、P8-06B 迁移原则

- 1 commit/1 scope；无 RuntimeManager / ThreadManager / WorkerRegistry
- Legacy PM wrapper 路径 identity 不变；patch seam `pm._ALGO_QUEUE`,
  `pm._ALGO_WORKER_STARTED`, `pm.threading.Thread`, `pm._ws_am_leader`,
  `pm._ws_listen_key` 等继续有效
- `runtime.py` 自身：零 import side effect（不 thread/redis/HTTP/sleep）
- deferred runtime state 明细：见 §九 KEEP/DEFERRED 表
