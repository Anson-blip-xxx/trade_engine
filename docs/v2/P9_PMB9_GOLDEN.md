# P9-02A — PMB-9 Algo Cancel Fallback Characterization（冻结当前 bug）

## 一、Cancel Call Graph（HEAD）
```
full close / ghost cleanup / monitor / update_stop
  → `_algo_place_sl_inner` / `_cancel_all_algo`（primary 走 light）
  `update_stop_loss`/lifecycle → `_algo_cancel(old_algo)`（cancel_id 通道）
  → PM `_algo_cancel` → `_s6api()[2]` → fapi_delete('/fapi/v1/algoOrder',{'algoId':..})
```

## 二、Tuple/slot contract（核心 bug 位）

`_s6api()` build tuple：
- **primary**（正常 import 路径）：
  `(fapi_get, fapi_post, fapi_delete, get_price, get_symbol_info, get_oi_and_funding, get_rsi, record_trade)`
  slot-3 = `shared.binance_api.fapi_delete`（DELETE 语义 ✅）
- **fallback tuple**（`from shared.binance_api ... except ImportError` 路径）：
  `(_light_fapi_get, _light_fapi_post, _light_fapi_get, _price, _info, _oif, _rsi, _rec)`
  **slot-3 错点 = `_light_fapi_get`** —— intended `_light_fapi_delete`
  （golden：`tuple8[2] is _light_fapi_get` + `is not _light_fapi_delete`）

## 三、Fallback Trigger（真实边界）

只有 `from shared.binance_api import ...` 抛异常（import/依赖缺失/网络时构造异常）→
进程 lazy 构造 fallback tuple。**primary success（有 binance_api）永不进这一路径**。

## 四、Endpoint/params（primary 与 fallback 同一 endpoint）

`_algo_cancel`（DELETE 通道）固定调 `/fapi/v1/algoOrder` `{'algoId': id}`，
method 决定位置在 slot —— fallback GET 是 transport 层错，非 endpoint 错。
`_cancel_all_algo` 冻结：fetch `/fapi/v1/allAlgoOrders`→`{'symbol': sym}`，
DELETE `/fapi/v1/algoOrder` `{'symbol': sym, 'algoId': id}`。

## 五、Status filter（PMB-15 冻结）

只处理 `NEW / WORKING / TRIGGERED`；FINISHED/EXPIRED skip。
`light get` 返回空/非 list → no-op。

## 六、Failure matrix（`_algo_cancel` exception）

- primary success → `return result`
- primary exception → `return {'error': str(e)}`；fallback tuple
  内 GET exception 也被 `_algo_cancel` 外层 try 吞 → `{'error': ...}`

## 七、Lifecycle blast radius（真实 caller）

`_algo_cancel` 只有 1 个 prod 调用点：`update_stop_loss`（be_done/cancel 旧 SL）
+ `_protection_service.cancel_id_fn`；
`_cancel_all_algo` 被 close/flat close 的「成功后再清理」使用。同为主 保护链。

## 八、Real risk vs plausible risk

- **confirmed**：fallback tuple 下 `_algo_cancel` 发的 GET 而非 DELETE →
  DELETE never happens（cancel 失败）→ `update_stop_loss` 老 SL
  → 由于 algoOrder 的 condition 已在交易所仍驻留——符合 stale 保护留置。
- **plausible**：重开时旧保护残留干扰触发错单。
- ghost cleanup 用 `record_trade`/`_cancel_all_algo`；来自 `_light_fapi_delete`
  （正确）—— ghost 不经过 fallback tuple → 不受 PMB-9 直接影响。

## 九、C1 分离

PMB-9 是行为 bug（ bicycle in fallback tuple 的槽位 pointer）；
C1 是 `_light_fapi` 整体合并 architecture cleanup。**不互相须**——
P9-02B 只改第三 slot 为 `_light_fapi_delete`（atom 一行）；不迁移 ownership。

## 十、P9-02B Intended Delta（计划）

```python
_S6_API = (_light_fapi_get, _light_fapi_post, _light_fapi_delete, ...)
```
同 endpoint 同 params（`/fapi/v1/algoOrder`）；无 auth/signature 变化；
无 caller 依赖 GET 返回 shape（fallback GET 侧只在 error dict path）。
单 commit revert，production diff = 1 行。


---

# FIXED（P9-02B）

- **FIXED BY:** commit `fix(v2): correct algo cancel fallback delete seam`（待填号）
- **OLD**: fallback tuple slot-3 = `_light_fapi_get` —— `_algo_cancel` 兜底模式发
  GET（delete 永未发生；stale 保护留置 confirmed）
- **NEW**: slot-3 = `_light_fapi_delete` —— 兜底 cancel 真删除
- **scope**: single tuple slot（`shared/position_manager.py` 一行）
- **primary path**: unchanged（`shared.binance_api.fapi_delete` 不动）
- **C1**: 仍 DEFERRED（_light_fapi 三 helper body 0 diff）
- **rollback**: `git revert <P9-02B_commit>` —— 恢复 GET bug；
  characterization/docs 保留
