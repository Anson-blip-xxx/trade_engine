"""API-key / auth 机械 helper（P8-04，C2；pure parse — transport/signing 不迁）。

GREEN 冻结（behavior 0 change）：
- env 文件解析：`config/binance.env` KB 逐行 `k=v`（strip/index）；
  `BINANCE_TESTNET` `true` 优先判定；key/secret 按段位（testnet 专用键）
- call-time lookup 语义冻结：PM `_ensure_apikey` 保留 `is None` 懒加载判别
  ——**没有** import-time capture；module globals 缓存 = HEAD 语义（单标注
  ——首次读取缓存，与 HEAD 一致）
- header 冻结：`{'X-MBX-APIKEY': api_key}` 逐字（缺 key 行为 = HEAD：
  caller 自己判定 `if not _API_KEY or not _API_SECRET` → None）
- 本 helper 不发请求、不签名、不缓存 credential、无 secret 泄漏
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def load_api_keys(env_path: Path) -> tuple:
    """从 binance.env 纯解析 (api_key, api_secret)；缺失 → (None, None)。

    HEAD `_ensure_apikey` 的循环逐字镜像：
    - `BINANCE_TESTNET` 出现后才确定段位（!的 gapping；缺 → 主网段）
    - 主网段：BINANCE_API_KEY + (BINANCE_SECRET_KEY | BINANCE_API_SECRET)
    - test 段：BINANCE_TESTNET_API_KEY + BINANCE_TESTNET_API_SECRET
    - 值 strip；`#…` 注释行 skip；`k=v` within 定位
    - 解析异常上抛（caller try）
    """
    api_key = None
    secret = None
    is_testnet = False
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                k = k.strip()
                if k == 'BINANCE_TESTNET':
                    is_testnet = v.strip().lower() == 'true'
                elif k == 'BINANCE_API_KEY' and not is_testnet:
                    api_key = v.strip()
                elif k in ('BINANCE_SECRET_KEY', 'BINANCE_API_SECRET') \
                        and not is_testnet:
                    secret = v.strip()
                elif k == 'BINANCE_TESTNET_API_KEY' and is_testnet:
                    api_key = v.strip()
                elif k == 'BINANCE_TESTNET_API_SECRET' and is_testnet:
                    secret = v.strip()
    return api_key, secret


def build_api_header(api_key) -> dict:
    """X-MBX-APIKEY header（逐字 name；value 原样透传）。"""
    return {'X-MBX-APIKEY': api_key}


def has_credentials(api_key, secret) -> bool:
    """presence check：`not key or not secret` 语义 == HEAD 原位判断。"""
    return bool(api_key) and bool(secret)
