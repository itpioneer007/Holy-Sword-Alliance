r"""安装/卸载「每日开盘冷启动链」Windows 计划任务 (2026-09-29)。

配合 scripts/daily_prep.py 使用。它解决的是用户提出的这条痛点:

    "我有时候会忘记在晚上 6 点预约启动怎么办, 我还要启动 MuMu 模拟器,
     点击游戏进行登录, 才能让你的脚本开始操作, 有点麻烦。"

装完之后, 人不用再做任何事 —— 到点自动: 启动 MuMu -> 等 adb -> 拉起游戏 ->
完成登录 -> 登记当日预约 (荣耀 18:00; 周末另加争霸 16:00)。

为什么是每天两次 (15:40 / 17:40)
    · 17:40 那次服务于**荣耀时刻** 18:00 窗口 (提前 20 分钟, 够冷启动 + 登录)。
    · 15:40 那次服务于**全民争霸** 16:00 窗口, 只在周六/日真正干活 —— 工作日
      这时的 `daily_prep.pick_targets()` 返回空, 脚本会在启动模拟器**之前**就退出,
      不会把 MuMu 和游戏空转两个半小时 (MuMu 内存压力下约每 7 分钟回收一次游戏
      进程, 空转越久越容易死)。

为什么不用 <EndBoundary>
    与看门狗同一教训: 旧版看门狗把 EndBoundary 设成"安装当晚", 次日任务永久失效
    (09-23 与 09-29 各中一次)。`ScheduleByDay` 下 `<EndBoundary>` 是**可选元素**,
    不写它就永久按日重复。装一次即可。

为什么 <StartWhenAvailable>true</StartWhenAvailable>
    若 17:40 机器恰好在睡眠/关机, 醒来后任务会被补跑一次。配合看门狗的电源保护
    (上班期间不会休眠), 双保险。

用法
    <venv python> scripts\install_prep_task.py             # 安装
    <venv python> scripts\install_prep_task.py --at 17:30  # 改主触发时刻
    <venv python> scripts\install_prep_task.py --check     # 查询
    <venv python> scripts\install_prep_task.py --remove    # 卸载
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_install_common import (  # noqa: E402
    query_task, register_task, remove_task, verify_next_run,
)

ROOT = Path(__file__).resolve().parent.parent
TASK_NAME = "SJBot_Daily_Prep"
PY = Path(sys.executable)
PREP = ROOT / "scripts" / "daily_prep.py"

# 固定锚点日期 (同看门狗): ScheduleByDay 下日期部分无意义, 写常量让 XML 可复现。
ANCHOR_DATE = "2026-01-01"

XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{now}</Date>
    <Author>{author}</Author>
    <Description>每日开盘冷启动链: 启动 MuMu -> 等 adb -> 拉起游戏 -> 完成登录 -> 登记当日预约(荣耀18:00/周末争霸16:00)。每天 {at1} 与 {at2} 各一次, 永久有效。有任务正在跑时脚本会自动跳过, 不干预对局。</Description>
    <URI>\\{task}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start1}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>{start2}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT20M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>"{prep}"</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def build_xml(at_arena: str, at_glory: str) -> str:
    user = os.environ.get("USERNAME", "")
    comp = os.environ.get("COMPUTERNAME", "")
    return XML.format(
        now=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        author=f"{comp}\\{user}",
        task=TASK_NAME,
        at1=at_arena,
        at2=at_glory,
        start1=f"{ANCHOR_DATE}T{at_arena}:00",
        start2=f"{ANCHOR_DATE}T{at_glory}:00",
        user=f"{comp}\\{user}",
        py=str(PY),
        prep=str(PREP),
        root=str(ROOT),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="17:40", help="荣耀(傍晚)那次触发时刻 (默认 17:40)")
    ap.add_argument("--at-arena", default="15:40", help="争霸(下午)那次触发时刻 (默认 15:40)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()

    if a.check:
        ok, out = query_task(TASK_NAME)
        print(out)
        print("\n" + verify_next_run(TASK_NAME))
        return 0 if ok else 1

    if a.remove:
        ok, out = remove_task(TASK_NAME)
        print(out)
        return 0 if ok else 1

    if not PREP.exists():
        print(f"缺少冷启动脚本: {PREP}")
        return 2
    if not PY.exists():
        print(f"缺少解释器: {PY}")
        return 2

    xml = build_xml(a.at_arena, a.at)
    print(f"任务 {TASK_NAME}:")
    print(f"  触发: 每天 {a.at_arena} 与 {a.at} (无 EndBoundary => 永久)")
    print(f"  动作: {PY} \"{PREP}\"  (cwd={ROOT})")
    print(f"  执行时限: PT20M   StartWhenAvailable: true (睡过的会补跑)")

    ok, msg = register_task(TASK_NAME, xml)
    print(msg)
    if ok:
        print("\n装完必看 (旧的看门狗就是 NextRun 为空才暴露的):")
        print("  " + verify_next_run(TASK_NAME))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
