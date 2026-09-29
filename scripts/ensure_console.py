r"""控制台看门狗 (2026-09-22) —— 由 Windows 计划任务在晚间窗口前/中调用。

为什么需要它
    正常启动的 5050 控制台是挂在某个交互进程下的 (start_console.bat 的 cmd 窗口,
    或调试会话的 shell)。那个进程一退出, 控制台跟着死, 已登记的「预约启动」
    (data/schedule.json) 就永远不会触发 —— 表现为"到点了什么都没发生"的静默失败。
    本脚本把控制台的生命周期交给计划任务实例持有, 不再依赖任何交互窗口。

幂等契约 (可以每分钟调用一次, 没有副作用)
    端口 5050 已通  -> 立刻退出 0, 绝不再起第二个实例
    端口 5050 未通  -> **前台**运行 `python -m sj_bot.server`

    前台而非后台是刻意的: 计划任务侧 MultipleInstances=IgnoreNew, 任务实例活着
    就等于服务活着; 若改成 spawn 分离子进程, 任务实例一结束就可能连带回收子进程,
    反而会让服务反复起停。

    用 python.exe 而非 pythonw.exe 也是刻意的: 引擎大量 subprocess 调用 adb.exe
    (screencap 每秒一次), 且未设 CREATE_NO_WINDOW —— 若父进程是 GUI 子系统的
    pythonw (无控制台), 每个 adb 子进程都会新建并弹出一个控制台窗口, 刷屏。

它还负责电源, 见 keep_awake(): 本机休眠超时 3 小时, 无人操作时会在 19:55 左右
把整台机器睡掉, 那正好是 18:00-20:00 挂机窗口的尾巴。

用法
    <venv python> scripts\ensure_console.py          # 计划任务调用
    <venv python> scripts\ensure_console.py --dry    # 只判定并打印, 不启动服务
    <venv python> scripts\ensure_console.py --port 5099 --dry
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 5050
LOG_FILE = ROOT / "data" / "console_guard.log"


def port_up(port: int, timeout: float = 1.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def note(msg: str) -> None:
    line = f"[guard {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass            # 日志失败不能影响拉起服务这个主职


def keep_awake() -> str:
    """按住系统空闲计时器, 阻止"无人操作 -> 到点休眠"。

    实机口径 (2026-09-22 powercfg /q SCHEME_CURRENT SUB_SLEEP):
        待机 STANDBYIDLE   = 0x0     从不
        休眠 HIBERNATEIDLE = 0x2a30  3 小时 <= 真正会咬人的那个: 最后一次人工输入
        之后 3 小时进入休眠, 若 16:55 离开就在 19:55 休眠 —— 恰好掐在 18:00-20:00
        窗口的尾部, 表现为"打了一半整个机器睡死"。

    ES_SYSTEM_REQUIRED 重置系统空闲计时器, 正是对付这个的官方手段; 本调用绑在
    守护线程上, 线程随进程结束而消失, 不留任何持久副作用 (不动用户的电源方案)。
    刻意不加 ES_DISPLAY_REQUIRED: 关屏不影响 adb 截图, 没必要亮屏耗电。

    注: pythonw/GUI 子系统下 windll 同样可用, 但要保持和控制台版一致的行为。
    """
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        rc = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return "已按住空闲计时器" if rc else f"SetThreadExecutionState rc={rc} (可能失败)"
    except Exception as e:                    # 非 Windows / 无 ctypes: 不该因此拒绝干活
        return f"不可用({e!r})"


def main(argv: list[str]) -> int:
    dry = "--dry" in argv
    port = DEFAULT_PORT
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])

    if port_up(port):
        note(f"OK 控制台已在 {port} 监听, 守护退出 (不重复拉起)")
        return 0

    cmd = [sys.executable, "-m", "sj_bot.server"]
    if dry:
        note(f"DRY {port} 未监听 -> 本应前台运行: {' '.join(cmd)} (cwd={ROOT})")
        return 0

    note(f"WARN {port} 未监听 -> 由守护拉起控制台并持有 (cwd={ROOT})")
    note(f"电源保护: {keep_awake()}")
    os.chdir(ROOT)
    try:
        with (ROOT / "srv_out.log").open("a", encoding="utf-8", errors="replace") as fo, \
             (ROOT / "srv_err.log").open("a", encoding="utf-8", errors="replace") as fe:
            rc = subprocess.call(cmd, stdout=fo, stderr=fe, cwd=str(ROOT))
        note(f"控制台进程退出 rc={rc} (计划任务实例结束)")
        return rc
    except OSError as e:                      # 解释器/路径异常: 必须留痕, 否则又是静默失败
        note(f"ERROR 拉起控制台失败: {e!r}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
