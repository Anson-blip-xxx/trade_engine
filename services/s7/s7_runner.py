#!/usr/bin/env python3
"""
s7_runner.py - 主循环 + 热reload检测（永不重启）
持有 infrastructure（core）和 strategy（logic），检测 logic 文件变更自动 reload
RuntimeState 常驻内存，每30分钟持久化一次，SIGTERM 时保存退出
"""
import time, importlib, os, signal
import s7_core as core
import s7_logic as logic


def main():
    symbols = [s.strip() for s in core.GRID_SYMBOLS if s.strip()]

    # 启动实时行情 Feed
    core._mdf = core.MarketDataFeed(symbols)
    core._mdf.start()
    time.sleep(2)

    # 启动 MarketGuard（已下沉到 s7_core，不受 logic reload 影响）
    core._guard = core.MarketGuard(symbols)
    core._guard.start()

    # 启动时加载一次 RuntimeState，之后常驻内存
    runtime_state = core.load_state()
    _last_save = time.time()

    # 注册退出信号处理器（SIGTERM / SIGINT 时保存状态）
    def _on_exit(sig, frame):
        core.save_state(runtime_state)
        core.log('[退出] 状态已保存')
        exit(0)
    signal.signal(signal.SIGTERM, _on_exit)
    signal.signal(signal.SIGINT, _on_exit)

    logic_mtime = os.path.getmtime(logic.__file__)
    core.log('s7 runner 启动（热更新架构 + 常驻内存状态）')
    core.tg('🔲 *s7 网格系统已启动*（热更新架构 + 常驻内存状态）')

    while True:
        try:
            runtime_state['s2_global_pause'] = False  # 每轮重置

            for symbol in symbols:
                try:
                    runtime_state = logic.manage_grid(symbol, runtime_state)
                except Exception as e:
                    core.log(f'[异常] {symbol}: {e}')

            core.log('[心跳] 网格巡检完成')

            # 每30分钟持久化一次
            if time.time() - _last_save >= 1800:
                core.save_state(runtime_state)
                _last_save = time.time()
                core.log('[持久化] RuntimeState 已写入磁盘')

            # 热更新检测
            new_mtime = os.path.getmtime(logic.__file__)
            if new_mtime != logic_mtime:
                core.save_state(runtime_state)  # reload 前先保存
                importlib.reload(logic)
                logic_mtime = new_mtime
                core.log('[热更新] reload 完成')
                core.tg('♻️ *s7 热更新* 策略逻辑已 reload')

            time.sleep(300)

        except Exception as e:
            core.log(f'[主循环异常] {e}')
            time.sleep(60)


if __name__ == '__main__':
    main()
