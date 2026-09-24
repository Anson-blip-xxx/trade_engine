# V2 测试网交易驾驶舱

地址：`https://43-133-4-231.sslip.io:9527/`。HTTPS 由 Nginx 处理，
全站 HTTP Basic 认证；用户名 `operator`，口令保存在服务器
`/var/lib/trade-engine-v2/dashboard-login.secret`，不进入 Git 或进程参数。

页面服务仅监听 `127.0.0.1:19527`，以独立 `tradev2dash` 系统用户运行。
PostgreSQL 同名 peer 角色只获指定表的 `SELECT`；默认事务只读，应用另显式
开启只读事务。该角色没有交易 API 密钥。9527 端口只提供 HTTPS。

数据语义：

- 开仓意图、订单、成交和交易详情来自 V2 PostgreSQL。
- 当前仓位按已登记 OPEN 成交量减 CLOSE 成交量计算，是账本状态，不是实时交易所
  仓位或浮动盈亏。交易所实时状态仍应查看 Binance 和独立对账。
- 已实现净收益来自每笔最新结算 revision 的 `net_pnl`，只包括 S6/S8 测试网
  交易；未结算的成交不计入收益。策略胜率以同一结算样本计。
- TradingView 信号出现在最新信号流中，只表示入库，不代表策略消费或下单。
- 页面显示时间统一转为 UTC+8；数据库 TIMESTAMPTZ 保留绝对时间。

部署模板位于 `deploy/v2-testnet/trade-v2-dashboard.service.in` 和
`deploy/v2-testnet/trade-v2-dashboard-nginx.conf`。更新版本时先运行 HTTP/DB
只读验证，再切换 systemd 的不可变 release 目录；Nginx 经 `nginx -t`
验证后平滑重载。
