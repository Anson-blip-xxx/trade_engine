"""PositionManager 域静态常量（P8-01，literal relocation；值逐字禁改）。

leaf module：仅 stdlib（本文件零 import）；import 无任何副作用。
  
冻结值（byte-for-byte，禁"集中优化"）：
- ws:leader / TTL 45；ghost lock TTL 60 / 0.001 阈值 / freshness** 30**/
  grace 30s / seen 24h / worker sleep 全部 inline 残留在 owner 模块，不迁
- 本文件只承载模块级 immutable 字面量：
  SYSTEM_CFG / _SYSTEM_KEYS / _WS_LEASE_KEY / _WS_LEASE_TTL /
  _API_COOLDOWN / _ALGO_UPDATE_INTERVAL / _ALGO_MIN_CHANGE_PCT

runtime mutable ownership（_WS_POSITIONS / _ALGO_QUEUE / 节流 dict /
lamzy init / env-derived）一律**不迁**——留在 shared/position_manager.py。
"""

from __future__ import annotations

SYSTEM_CFG = {
    'S8A': {
        'be_done_threshold': 2.0,      # 浮盈≥% 触发止损移到成本
        'trail': {
            'base_mult': 0.3,          # 基础追踪间距 (×ATR)
            'tighten_pct': 8.0,        # 浮盈≥此值开始收紧
            'tighten_min': 0.5,        # 最紧间距倍率
            'min_profit_lock_pct': 2.0,
            'breakeven_atr': 1.5,      # 浮盈≥此值×ATR → 保本加固
        },
        'time_stop_min': 240,          # 时间止损（分钟）
        'time_extend_min': 60,         # 可延期（分钟）
        'extend_rsi_min': 60,          # 延期条件：RSI > 此值
        'extend_funding_min': 0.0005,  # 延期条件：资金费 > 此值
        'sl_breach_max': -5.0,         # 紧急止损（%）
        'partial_tp': {5: 0.3},        # 浮盈≥% → 平仓比例
        'peak_guard': {                # 峰值回撤保护：防回踩拉升吞掉浮盈
            'trigger_pct': 3.0,        # 浮盈≥% 后启动，实时上移锁利止损
            'drawdown_pct': 2.0,       # 从峰值回撤≥% → 立即平仓锁利
        },
    },
    'S8B': {
        'be_done_threshold': 3.0,
        'trail': {
            'base_mult': 0.3,
            'tighten_pct': 8.0,
            'tighten_min': 0.5,
            'min_profit_lock_pct': 2.0,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 300,
        'time_stop_fast_min': 150,
        'funding_fast_threshold': -0.00015,
        'partial_tp': {8: 0.3},
        'peak_guard': {'trigger_pct': 3.5, 'drawdown_pct': 2.5},
    },
    'S6A': {
        'be_done_threshold': 2.5,
        'trail': {
            'base_mult': 0.3,
            'tighten_pct': 5.0,        # 普通做多浮盈保护更积极
            'tighten_min': 0.3,
            'min_profit_lock_pct': 2.0,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 120,
        'partial_tp': {5: 0.5},
        'peak_guard': {'trigger_pct': 3.0, 'drawdown_pct': 2.0},
    },
    'S6B': {
        'be_done_threshold': 5.0,      # 趋势接管给趋势更多空间
        'trail': {
            'base_mult': 0.6,
            'tighten_pct': 10.0,
            'tighten_min': 0.8,
            'min_profit_lock_pct': 2.0,
            'max_drawdown_pct': 20.0,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 480,
        'partial_tp': {8: 0.3},
        'peak_guard': {'trigger_pct': 5.0, 'drawdown_pct': 3.0},
    },
    # 旧 S6 兜底（存量仓位可能还是 system='S6'）
    'S6': {
        'be_done_threshold': 2.5,
        'trail': {
            'base_mult': 0.3,
            'tighten_pct': 5.0,
            'tighten_min': 0.3,
            'min_profit_lock_pct': 2.0,
            'breakeven_atr': 1.5,
        },
        'time_stop_min': 120,
        'partial_tp': {5: 0.3},
        'peak_guard': {'trigger_pct': 3.0, 'drawdown_pct': 2.0},
    },
}

_SYSTEM_KEYS = {
    'S6': 'state:s6',
    'S8': 'state:s8',
}

# WS leader lease（值逐字；原 PM alias 保留为测试 seam）
_WS_LEASE_KEY = 'ws:leader'
_WS_LEASE_TTL = 45

# 同标的 API 最小间隔（秒）/ AlgoSL 更新最少间隔 / SL 变化阈值（%）
_API_COOLDOWN = 3
_ALGO_UPDATE_INTERVAL = 60
_ALGO_MIN_CHANGE_PCT = 0.2
