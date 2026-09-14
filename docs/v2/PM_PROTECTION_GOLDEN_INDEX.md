# PM Protection Golden Index（P7-04A）

> 映射 Golden ID → protection area → test file → P7-04B Protection Service 约束。
> 原始细节：PROTECTION_MONITORING_INVENTORY.md / PM_GOLDEN_OBSERVATIONS.md。

| Golden ID | Area | Observed Behavior | Test file | P7-04B constraint |
|-----------|------|-------------------|-----------|---------------------|
| PMB-8 | Algo Cancel 覆盖矩阵 | rejected 不 cancel / partial / ghost 不 cancel | test_protection_queue_golden.py / test_layout | P7-04B 分支保护冻结 |
| PMB-9 | _algo_cancel fallback GET | `_s6api` 兜底第 3 槽 = `_light_fapi_get` → GET 冒 DELETE | test_protection_cancel_golden.py::test_fallback_mode_get_not_delete_frozen | 不修——P7-04B port 内补真 DELETE wrapper 或与 record_trade 内嵌同步 |
| PMB-N1 | se import 副作用 | `_algo_start_worker()` at se module import | test_protection_worker_golden.py::test_import_side_effect_frozen | P7-04B 拆后禁止多份 spawn |
| E-OBS-n (2) | 队列 item shape | `(sym, side, trigger, qty)` 4-tuple（非 dict） | test_protection_queue_golden.py::test_queue_item_is_exactly_4_tuple | P7-04B 不改货物 shape |
| E-OBS-n (3) | FIFO | A then B → consume A then B | TestQueueSemantics | 不得换 priority |
| E-OBS-n (4) | Restart loss | 重启 → 队列内容丢失 | TestBreadthQueueRestart | 不添加 persistence |
| E-OBS-n (5) | 11s 限速窗 | enqueue → sleep(11) 期间 SL 缺失 | test_protection_worker_golden.py::TestNoSLWindow | 不优化为同步 |
| E-OBS-n (6) | exchangeInfo | raw requests + LOT_SIZE/PRICE_FILTER tick 逼进 | test_protection_failure_golden.py::test_tick_size_price_round | 精度规则冻结 |
| E-OBS-n (7) | place 单次（无 retry/requeue） | place 失败 → job drop | test_protection_worker_golden.py | P7-04B 不得加 retry |
| row (8) | algo_sl_id write | 成功 → 写 `pm:positions`；失败 → 不写 | test_protection_worker_golden.py::TestAlgoPlaceResult | state write 语义保持 |
| row (9) | cancel-all 在 place 前 | cancel → place 顺序 | test_protection_cancel_golden.py | P7-04B 保持顺序 |
| row (10) | double-start 无效 | `_ALGO_WORKER_STARTED=True` 每次 start 只启 1 份 | TestWorkerStartSemantics | P7-04B 不得多余线程 |
| ProtectionPort | 5-method sufficiency | enqueue/start/place/cancel_id/cancel_all | test_protection_failure_golden.py::TestProtectionPortFrozen | 未扩 port |
