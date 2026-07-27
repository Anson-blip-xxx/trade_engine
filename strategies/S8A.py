#!/usr/bin/env python3
"""S8A shim — 兼容旧systemd名称，实际执行S8"""
import sys
sys.path.insert(0, ".")
from trading_engine.strategies.S8 import main
main()
