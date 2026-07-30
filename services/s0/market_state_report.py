#!/usr/bin/env python3
"""P1 市场状态日报 — 状态占比 + s6 盈亏归因"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shared.clickhouse_client import query as _chq

TG_BOT  = "8488191250:AAEMOmEY3UZE0WtCn-3Ywn-2ESje5eIKngY"
TG_CHAT = "5709781617"

def ch(sql):
    r = _chq(sql)
    return [list(row) for row in r] if r else []

def tg(msg):
    import requests
    requests.post(f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
                  data={"chat_id": TG_CHAT, "text": msg}, timeout=5)

# 过去7天市场状态占比
rows = ch("""
    SELECT market_state, count() as cnt,
           round(count()*100.0/sum(count()) OVER(),1) as pct
    FROM default.market_state_log
    WHERE ts >= now() - INTERVAL 7 DAY
    GROUP BY market_state ORDER BY cnt DESC
""")
state_lines = "\n".join(f"  {r[0]:<10} {r[1]:>5}次  {r[2]}%" for r in rows) if rows else "  暂无数据"

# 过去7天 s6 按市场状态的盈亏
rows2 = ch("""
    SELECT market_state_entry, count() as trades,
           countIf(result='win') as wins,
           round(countIf(result='win')*100.0/count(),1) as wr,
           round(sum(pnl_usdt),2) as pnl
    FROM default.trade_history
    WHERE trade_time >= now() - INTERVAL 7 DAY
      AND market_state_entry != ''
    GROUP BY market_state_entry ORDER BY pnl DESC
""")
pnl_lines = "\n".join(
    f"  {r[0]:<10} {r[1]}单 胜率{r[3]}% PnL {r[4]}U" for r in rows2
) if rows2 else "  暂无交易数据"

# 过去7天 s6 按信号类型的盈亏
rows3 = ch("""
    SELECT signal_type, count() as trades,
           round(countIf(result='win')*100.0/count(),1) as wr,
           round(sum(pnl_usdt),2) as pnl
    FROM default.trade_history
    WHERE trade_time >= now() - INTERVAL 7 DAY
      AND signal_type != ''
    GROUP BY signal_type ORDER BY pnl DESC
""")
sig_lines = "\n".join(
    f"  {r[0]:<8} {r[1]}单 胜率{r[2]}% PnL {r[3]}U" for r in rows3
) if rows3 else "  暂无数据"

msg = f"""📊 市场状态日报（近7天）

【状态分布】
{state_lines}

【s6 按市场状态盈亏】
{pnl_lines}

【s6 按信号类型盈亏】
{sig_lines}"""

print(msg)
tg(msg)
