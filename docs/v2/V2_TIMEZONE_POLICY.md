# V2 时间存储与 UTC+8 展示契约

V2 的人类可读时间统一使用 `Asia/Shanghai / UTC+8`，但不会给真实时间戳机械加八小时。

## 存储规则

- Binance、行情和业务证据继续保存 Unix epoch 毫秒。epoch 表示绝对时刻，不带展示时区。
- PostgreSQL 继续使用 `TIMESTAMPTZ`。它内部保存绝对时刻；V2 连接工厂强制会话
  `TimeZone=Asia/Shanghai`，所以 SQL 文本和客户端读取的时间显示为 `+08`。
- ClickHouse 原始归档仍保存原始 payload/epoch；服务配置显式设置
  `timezone=Asia/Shanghai`，启动 preflight 会验证该值。
- 排序、租约、超时、K 线边界、幂等键和 Binance 签名时间全部继续按绝对时间或 epoch
  计算，不使用格式化后的本地时间参与判断。

## 展示规则

- TG 开仓和平仓消息使用固定 `UTC+8` 转换并明确显示后缀。
- API/JSON 中名称带 `_ms` 的字段仍是 epoch 毫秒，不伪装成本地时间字符串。
- PG 运维查询通过 V2 连接或数据库默认配置显示 `Asia/Shanghai`；导出到其他客户端时，
  客户端仍应明确设置 `TimeZone=Asia/Shanghai`。

## 历史数据

现有 epoch 和 `TIMESTAMPTZ` 数据不迁移、不重写。它们已经表示正确的绝对时刻；重写会
人为制造八小时偏差。此次变更只统一会话、服务和用户消息的展示时区。
