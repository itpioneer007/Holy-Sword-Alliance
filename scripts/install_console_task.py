r"""安装/卸载「控制台看门狗」Windows 计划任务。

背景: 预约启动 (data/schedule.json) 只在控制台进程活着时才有效。若控制台挂在
交互窗口/调试会话下, 那个进程一退出就是个静默失败。本脚本把一个**永久有效**的
计划任务装进 Task Scheduler, 周期性调用 scripts/ensure_console.py: 控制台一掉就
被拉回来, 并由任务实例持有其生命周期。

2026-09-29 修复 (原设计每天失效一次)
    旧版把 <EndBoundary> 设成"安装时刻 + hours 小时 + 1 分钟", 于是一个触发器
    只护住**安装当晚**那一段。次日 EndBoundary 已成过去时, NextRun 为空, 任务
    再也不触发 —— 实测 09-23 与 09-29 各中一次, 表现为"看门狗静默消失"。
    旧版 build_xml() 的 docstring 自己也写了"明天不再响", 属已知未修。

    修法: 在 <ScheduleByDay><DaysInterval>1</DaysInterval> 下, <EndBoundary> 是
    **可选元素**。删掉它, 触发器就永久按日重复 —— 装一次即可, 不再需要每天重装。

三处关键设置 (都有踩坑背景, 别随意改回)
    · Repetition 覆盖全天 (默认 PT10M × 24h): 控制台若在凌晨挂掉, 10 分钟内就被
      拉回, 不留"到傍晚才有人管"的空洞。
    · <LogonTrigger Delay=PT2M>: 覆盖重启/重新登录场景。等 2 分钟是为了让
      Windows 把 adb、网络等依赖先就绪。
    · <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>: 0 = 不限。守护进程是**前台
      持有**控制台的 (见 ensure_console.py 的幂等契约), 若给执行时限, 到点任务实例
      被终止会连带把控制台一起杀掉 (实测日志 "控制台进程退出 rc=1 (计划任务实例
      结束)")。所以必须放开。

⚠️ 副作用 (有意为之, 知情后可选): 守护持有控制台期间会调 SetThreadExecutionState
   (ES_SYSTEM_REQUIRED) 按住系统空闲计时器, 阻止"无人操作 3 小时 -> 休眠"。本机
   HIBERNATEIDLE=3h, 不按住就会在 19:55 前后睡死, 正好掐掉 18:00-20:00 窗口的尾巴。
   代价是机器基本不会因空闲而休眠 —— 这换取的是"人不在也能跑完整晚"。不想常驻就
   把 --duration 调小 (例如 6), 守护只在傍晚那几小时生效。

用法
    <venv python> scripts\install_console_task.py                # 安装: 每日 00:00 起每 10 分钟
    <venv python> scripts\install_console_task.py --start 17:00  # 只从 17:00 开始轮询
    <venv python> scripts\install_console_task.py --duration 6   # 每天只覆盖 6 小时 (傍晚型)
    <venv python> scripts\install_console_task.py --check        # 只查询
    <venv python> scripts\install_console_task.py --remove       # 卸载
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
TASK_NAME = "SJBot_Console_Guard"
PY = Path(sys.executable)
GUARD = ROOT / "scripts" / "ensure_console.py"

# 触发器的"锚点日期"固定成一个过去的常量日期, 而不是"今天"。
# 理由: 用 ScheduleByDay + DaysInterval=1, StartBoundary 只决定**每天几点**, 日期部分
# 无意义。写成固定常量可以让 XML 完全可复现 (同样的参数 -> 同样的 XML), 也顺带避免
# "安装时刻落在过去"这种会让任务立即补跑一次的边界情况。
ANCHOR_DATE = "2026-01-01"

XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{now}</Date>
    <Author>{author}</Author>
    <Description>看门狗: 每 {every} 分钟确认 5050 控制台在跑, 掉了就拉起并由本任务实例前台持有, 保证 data/schedule.json 里的预约能触发。触发器永久按日重复(无 EndBoundary), 另有登录触发器覆盖重启。注意: 出现两个 python 进程是正常的 (4MB 那个是 venv 父进程)。</Description>
    <URI>\\{task}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
      <Repetition>
        <Interval>PT{every}M</Interval>
        <Duration>PT{duration}H</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
      <Delay>PT2M</Delay>
    </LogonTrigger>
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
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>"{guard}"</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def build_xml(start_at: datetime, every_min: int, duration_h: float) -> str:
    """生成计划任务 XML。

    为什么是 ScheduleByDay 而不是"一次性"触发器: schtasks 对 CalendarTrigger 强制
    要求日/周/月调度元素, 纯 StartBoundary+Repetition 会被拒 (报
    `(18,8):StartBoundary:` 缺必需属性); 一次性触发器是 TimeTrigger, 但它不支持
    Repetition (就无法"每 10 分钟看一次")。故用日调度 + 无限重复。

    为什么**不写** EndBoundary: 见模块 docstring —— 写了就只护一天。
    """
    user = os.environ.get("USERNAME", "")
    comp = os.environ.get("COMPUTERNAME", "")
    return XML.format(
        now=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        author=f"{comp}\\{user}",
        task=TASK_NAME,
        start=start_at.strftime("%Y-%m-%dT%H:%M:%S"),
        every=int(every_min),
        duration=duration_h,
        user=f"{comp}\\{user}",
        py=str(PY),
        guard=str(GUARD),
        root=str(ROOT),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="00:00", help="每日轮询起始时刻 HH:MM (默认 00:00 = 全天)")
    ap.add_argument("--every", type=int, default=10, help="轮询间隔分钟 (默认 10)")
    ap.add_argument("--duration", type=float, default=24,
                    help="每日覆盖小时数 (默认 24 = 全覆盖; 傍晚型可设 6)")
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

    if not GUARD.exists():
        print(f"缺少守护脚本: {GUARD}")
        return 2
    if not PY.exists():
        print(f"缺少解释器: {PY}")
        return 2

    hh, mm = (int(x) for x in a.start.split(":"))
    start_at = datetime.strptime(f"{ANCHOR_DATE} {hh:02d}:{mm:02d}:00", "%Y-%m-%d %H:%M:%S")

    xml = build_xml(start_at, a.every, a.duration)
    print(f"任务 {TASK_NAME}: 每日 {a.start} 起每 {a.every} 分钟一次, 覆盖 {a.duration}h"
          + ("  (=全天常驻)" if a.duration >= 24 else ""))
    print(f"  触发器: 日调度(DaysInterval=1, 无 EndBoundary => 永久) + 登录后 2 分钟")
    print(f"  执行时限: PT0S (不限, 否则会连带杀掉前台持有的控制台)")
    print(f"  动作: {PY} \"{GUARD}\"  (cwd={ROOT})")

    ok, msg = register_task(TASK_NAME, xml)
    print(msg)
    if ok:
        print("\n装完必看 (旧版就是 NextRun 为空才暴露的):")
        print("  " + verify_next_run(TASK_NAME))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
