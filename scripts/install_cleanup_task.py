r"""安装/卸载「每月截图清理」Windows 计划任务 (2026-09-29)。

配合 scripts/cleanup_captures.py 使用。解决的是用户这句话:

    "之后我想要实现图片的每个月清一次, 我只需要看开宝箱的奖励是什么就行。"

`captures/` 里除了**开箱奖励图** (那是"我拿到了什么"的凭据, 永久留), 其余都是异常现场
截图 —— 每张 2.7MB 的全屏图, 一天十几张就是几十 MB。所以每月 1 号凌晨跑一次:

    captures/rewards/**   永久保留
    captures/ 其它       超过 --days 天删
    outputs/              临时调试产物, 同样按天清

为什么选凌晨 03:30
    避开通盘三个自动时段 (争霸 周末 16:00-17:00 / 荣耀 18:00-20:00 / 排位随时)。而且此刻
    引擎几乎不会在跑 (用户白天/晚上才用), 不会与对局抢 adb 或截图目录。

为什么 <StartWhenAvailable>true
    1 号凌晨机器常常是关的/睡着的。开了它, 开机后 Windows 会补跑一次 —— 否则"每月清一次"
    会变成"每个月恰好开机的那一夜才清"。

为什么不用 <EndBoundary>
    与看门狗、冷启动链同一教训 (09-23 与 09-29 各中一次): 写了 EndBoundary 就只护一个窗口,
    次月永久失效。`ScheduleByMonth` 下它是可选元素, 不写即永久按年重复。

用法
    <venv python> scripts\install_cleanup_task.py                # 安装 (保留 30 天)
    <venv python> scripts\install_cleanup_task.py --days 45      # 改保留期
    <venv python> scripts\install_cleanup_task.py --at 04:00     # 改时刻
    <venv python> scripts\install_cleanup_task.py --check        # 查询
    <venv python> scripts\install_cleanup_task.py --remove       # 卸载
    <venv python> scripts\install_cleanup_task.py --run-now      # 立刻手动跑一次
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_install_common import (  # noqa: E402
    query_task, register_task, remove_task, run, verify_next_run,
)

ROOT = Path(__file__).resolve().parent.parent
TASK_NAME = "SJBot_Monthly_Cleanup"
PY = Path(sys.executable)
CLEAN = ROOT / "scripts" / "cleanup_captures.py"

# 固定锚点日期: ScheduleByMonth 下年份无意义, 写成常量让 XML 可复现
ANCHOR_DATE = "2026-01-01"

XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{now}</Date>
    <Author>{author}</Author>
    <Description>每月 {day} 号 {at} 清理运行期截图: captures/ 下超过 {days} 天的异常现场图与 outputs/ 临时产物一并删除, 但 captures/rewards/ 里的开箱奖励图永久保留。永久有效 (无 EndBoundary)。</Description>
    <URI>\\{task}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth>
          <Day>{day}</Day>
        </DaysOfMonth>
        <Months>
          <January/><February/><March/><April/><May/><June/>
          <July/><August/><September/><October/><November/><December/>
        </Months>
      </ScheduleByMonth>
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
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>"{clean}" --days {days}</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def build_xml(at: str, day: int, days: int) -> str:
    user = os.environ.get("USERNAME", "")
    comp = os.environ.get("COMPUTERNAME", "")
    return XML.format(
        now=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        author=f"{comp}\\{user}",
        task=TASK_NAME,
        at=at,
        day=day,
        days=days,
        start=f"{ANCHOR_DATE}T{at}:00",
        user=f"{comp}\\{user}",
        py=str(PY),
        clean=str(CLEAN),
        root=str(ROOT),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="03:30", help="触发时刻 (默认 03:30)")
    ap.add_argument("--day", type=int, default=1, help="每月几号 (默认 1)")
    ap.add_argument("--days", type=int, default=30, help="保留多少天 (默认 30)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--run-now", action="store_true", help="立刻跑一次清理脚本 (不建任务)")
    a = ap.parse_args()

    if a.run_now:
        r = run(str(PY), str(CLEAN), "--days", str(a.days), "--dry")
        print("--dry 预览:\n" + (r.stdout or r.stderr))
        return 0

    if a.check:
        ok, out = query_task(TASK_NAME)
        print(out)
        print("\n" + verify_next_run(TASK_NAME))
        return 0 if ok else 1

    if a.remove:
        ok, out = remove_task(TASK_NAME)
        print(out)
        return 0 if ok else 1

    if not CLEAN.exists():
        print(f"缺少清理脚本: {CLEAN}")
        return 2
    if not PY.exists():
        print(f"缺少解释器: {PY}")
        return 2

    xml = build_xml(a.at, a.day, a.days)
    print(f"任务 {TASK_NAME}:")
    print(f"  触发: 每月 {a.day} 号 {a.at}  (无 EndBoundary => 永久)")
    print(f"  动作: {PY} \"{CLEAN}\" --days {a.days}  (cwd={ROOT})")
    print(f"  保留: captures/rewards/ 永久; 其余超过 {a.days} 天删; outputs/ 同规则")
    print("  执行时限: PT1H   StartWhenAvailable: true (关机错过的会补跑)")

    ok, msg = register_task(TASK_NAME, xml)
    print(msg)
    if ok:
        print("\n装完必看 (看门狗就是 NextRun 为空才暴露的):")
        print("  " + verify_next_run(TASK_NAME))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
