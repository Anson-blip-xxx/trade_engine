# PM Monitor / WS Golden Index — P7-05A

> 冻结 Position Manager 监控链（monitor_all / _monitor_one / WS / leader lease /
> ghost 队列 / reconcile）的具体行为，作为 P7-05B（Monitoring/WS Service 抽取）
> 的迁移约束。生产代码零改动；测试 + 文档。

## 测试文件 → 冻结行为

| 文件 | 测试数 | 冻结范围 |
|---|---|---|
| `tests/position_manager/test_monitor_all_golden.py` | 20 | monitor_all 编排（心跳节流/ghost Step 0/filter/_monitor_one 循环/ghost 队列消费/双 save/summary） |
| `tests/position_manager/test_monitor_one_golden.py` | 43 | _monitor_one 11 步出场链 + 各步 freeze（见下） |
| `tests/position_manager/test_monitor_leader_golden.py` | 10 | ws:leader lease 语义 + connect_loop 门控 |
| `tests/position_manager/test_monitor_ws_golden.py` | 9 | ACCOUNT_UPDATE 快照/平仓记账顺序 |
| `tests/position_manager/test_monitor_failure_golden.py` | 6 | 失败拓扑（reconcile skip / funding→0 / ghost 吞错） |

## _monitor_one 出场链（11 步，逐字顺序）

```
0  资金费率强平  SHORT fund<-0.5% / LONG fund>+0.5% → close '资金费率过高'
   警告级 ±0.2% → pos['fund_warned'] 一次性
1  硬止损        pos['sl']≠entry 且穿越 → '硬止损'（优先级最高）
2  紧急止损      pnl < cfg.sl_breach_max(-5.0)（严格 <）
3  早期亏损保护  hold≥5 且 pnl≤-2 → 15m 动量确认（异常吞错续链）
4  低收益停滞    _is_stagnant_profit(pnl_usdt, hold)
   3.5 be_done   pnl≥cfg.be_done_threshold → _update_stop_loss 一次
   4.5 分层止盈  阈值升序、每轮仅一层、tp_done 记账、出货保护(qty>close_qty)
5  追踪锁利      be_done 且 pnl≥be → _calc_trail_sl
                  'exit' → close '趋势反转（2次收>EMA20）' 返回 '趋势反转'
                  float → _place_trail_sl
5.5 峰值回撤     str → close；float → _place_trail_sl；None → 无动作
6  1h EMA 安全阀 hold<60 或 pnl≥40 豁免；反转 → _should_exit_1h_reversal
                  → 平/观察一次（warn-once）；命中即 return None（阻断第 7 步）
7  时间止损      hold>cfg.time_stop_min
                  浮亏 → 延期一次（RSI>funding 双门），time_extended 后直接平
                  微盈(0≤pnl<be) → 直接平
返回 tuple: (reason, price, entry, qty, side)
```

## monitor_all 编排序

```
_load() → [空仓: 60s 心跳节流, return []]
heartbeat (>60s): 逐 symbol 快照行（≤8 个，吞 get_price 异常）
Step 0: _ghost_cleanup(positions, system_filter)   ← filter 之前全量执行
filter: system_filter 前缀匹配
loop: _monitor_one(symbol, pos, all_positions)；异常 per-symbol 吞错续扫
ghost 队列消费: _RECENTLY_GHOSTED pop-循环按 side 匹配 system_filter
     （len<6 → side=None → 直接消费，PMB-17）
_save(all_positions)（全量，PMB-19）
closed 非空 → log_position_summary()
返回 closed 列表 [(symbol, reason, ...), ...]
```

## WS / leader 冻结

- `'ws:leader'`（逐字 key）、TTL 45、`_WS_INSTANCE = pid-hex8`
- `_ws_am_leader`: 自持→renew+True；空→acquire+True；他人→False；异常→True（fail-open / PMB-20）
- `_ws_on_message`: 仅 ACCOUNT_UPDATE；|pa|<0.001 → pop + （未标记时）record→mark（PMB-21）；新仓 schema 逐字；锁内刷新 `_WS_LAST_UPDATE`
- connect loop: 非领导者 sleep(5)；listenKey 空 → sleep(5)；`run_forever(ping_interval=30, ping_timeout=10)`

## 关联观察（PMB-16 ~ PMB-22）

详见 `PM_GOLDEN_OBSERVATIONS.md`：
- PMB-16 心跳节流合并；PMB-17 ghost side 缺失 filter 失效；PMB-18 延期一轮失效
- PMB-19 全量保存 + 双 save；PMB-20 lease fail-open；PMB-21 record→mark 顺序
- PMB-22 1h 反转 return None 阻断

## P7-05B 迁移约束（一句话）

Monitoring/WS 职责拆为独立 service 时：**上述编排顺序、节流全局、ghost 队列
消费规则、出场链步序、return 语义必须逐字保留**；任何"顺手修复"须作为
action ticket 显式决策（P7-00 规则）。
