r"""安装/卸载「每日截图清理」Windows 计划任务 (2026-09-29)。

配合 scripts/cleanup_captures.py 使用。解决的是用户这两句话:

    "之后我想要实现图片的每个月清一次, 我只需要看开宝箱的奖励是什么就行。"
    "那我应该怎么样维持这个文件的体积不要太大?"          (2026-09-29 追加)

## 为什么从"每月 1 号"改成"每天"

原设计每月 1 号清一次, 隐含假设是"一个月涨的量很小, 攒着清无所谓"。当天实测把这个
假设推翻了: 09-29 一天写了 **57 张 / 86.8 MB** 奖励图。月清 = 一个月可能攒到 2.6 GB
才动手, 期间体积完全失控 —— 而"失控"正是用户要避免的。

改成每天 03:30 之后, 任何一天的最坏情况都只是"多出一整天的量"。

## 三层闸门里这一层管什么

    1. 源头减量 : 奖励图存 JPEG q88 (省 88%)              <- battler.py
    2. 按龄回收 : 异常图 30 天 / 奖励图 90 天             <- --days / --rewards-days
    3. 硬上限   : captures/ 超 --max-mb 就从最旧删         <- --max-mb

第 3 层是这次新增的兜底: 保留期按**时间**设闸, 挡不住**速率**突变 (落盘格式/截图频率/
分辨率一改, 同一个保留期就从 1 GB 变 30 GB)。上限才是几何上的"不会爆"。

    --max-mb 2000 的来历: 稳态估算 ≈ rewards(60 局/天 x 0.17MB x 90 天 = 0.92GB)
    + 异常图(30 天 x ~11MB/天 = 0.33GB) ≈ 1.25 GB, 取 2000 留约 1.6 倍余量 ——
    正常情况永不触发, 只有速率真出问题时才动手。经验法则: **上限 >= 稳态 x 1.5**,
    调大 --rewards-days 时要把 --max-mb 一起调大, 否则保留期会被上限架空。

## 保留策略

    captures/ 其它        超过 --days 天删 (默认 30)
    captures/rewards/**   超过 --rewards-days 天删 (默认 90)
    outputs/              临时调试产物, 按 --days 清

奖励图**不是永久保留**, 因为"拿到了什么"这件事已经以文字落在 `data/rewards.jsonl`
(物品名 + 胜负 + 时间), 那份记录永不删、几乎不占空间; 图只是佐证, 留一个季度足够回看。

## 为什么选凌晨 03:30

避开通盘三个自动时段 (争霸 周末 16:00-17:00 / 荣耀 18:00-20:00 / 排位随时)。而且此刻
引擎几乎不会在跑 (用户白天/晚上才用), 不会与对局抢 adb 或截图目录。

## 为什么 <StartWhenAvailable>true

凌晨机器常常是关的/睡着的。开了它, 开机后 Windows 会补跑一次 —— 否则"每天清一次"
会变成"恰好开机的那一夜才清"。

## 为什么不用 <EndBoundary>

与看门狗、冷启动链同一教训 (09-23 与 09-29 各中一次): 写了 EndBoundary 就只护一个窗口,
次日永久失效。`ScheduleByDay` 下它是可选元素, 不写即永久按日重复。

用法
    <venv python> scripts\install_cleanup_task.py                    # 安装 (30/90 天, 上限 2000MB)
    <venv python> scripts\install_cleanup_task.py --days 45          # 改异常图保留期
    <venv python> scripts\install_cleanup_task.py --rewards-days 180 # 改奖励图保留期
    <venv python> scripts\install_cleanup_task.py --max-mb 3000      # 改硬上限
    <venv python> scripts\install_cleanup_task.py --at 04:00         # 改时刻
    <venv python> scripts\install_cleanup_task.py --check            # 查询
    <venv python> scripts\install_cleanup_task.py --remove           # 卸载
    <venv python> scripts\install_cleanup_task.py --run-now          # 立刻手动跑一次 (--dry)
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
TASK_NAME = "SJBot_Daily_Cleanup"
LEGACY_TASK = "SJBot_Monthly_Cleanup"        # 旧名 (每月 1 号) —— 装新的时顺手卸掉
PY = Path(sys.executable)
CLEAN = ROOT / "scripts" / "cleanup_captures.py"

# 固定锚点日期: 按日重复时年份无意义, 写成常量让 XML 可复现
ANCHOR_DATE = "2026-01-01"

XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{now}</Date>
    <Author>{author}</Author>
    <Description>每天 {at} 清理运行期截图: captures/ 下超过 {days} 天的异常现场图与 outputs/ 临时产物一并删除; 开箱奖励图另按 {rdays} 天保留 (物品名另存在 data/rewards.jsonl, 永不删); 再加一层 {cap} MB 硬上限兜底。永久有效 (无 EndBoundary)。</Description>
    <URI>\\{task}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
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
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>"{clean}" --days {days} --rewards-days {rdays} --max-mb {cap}</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def build_xml(at: str, days: int, rdays: int, cap: int) -> str:
    user = os.environ.get("USERNAME", "")
    comp = os.environ.get("COMPUTERNAME", "")
    return XML.format(
        now=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        author=f"{comp}\\{user}",
        task=TASK_NAME,
        at=at,
        days=days,
        rdays=rdays,
        cap=cap,
        start=f"{ANCHOR_DATE}T{at}:00",
        user=f"{comp}\\{user}",
        py=str(PY),
        clean=str(CLEAN),
        root=str(ROOT),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="03:30", help="触发时刻 (默认 03:30)")
    ap.add_argument("--days", type=int, default=30, help="异常图保留天数 (默认 30)")
    ap.add_argument("--rewards-days", type=int, default=90, help="奖励图保留天数 (默认 90)")
    ap.add_argument("--max-mb", type=int, default=2000,
                    help="captures/ 总量硬上限 MB (默认 2000; 0 = 不设)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--run-now", action="store_true", help="立刻跑一次清理脚本 (--dry 预览)")
    a = ap.parse_args()

    if a.run_now:
        extra = ["--max-mb", str(a.max_mb)] if a.max_mb > 0 else []
        r = run(str(PY), str(CLEAN), "--days", str(a.days),
                "--rewards-days", str(a.rewards_days), *extra, "--dry")
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
        ok2, out2 = remove_task(LEGACY_TASK)          # 旧名一起收掉
        if "不存在" not in out2 and "not" not in out2.lower():
            print(f"[旧任务 {LEGACY_TASK}] {out2}")
        return 0 if ok else 1

    if not CLEAN.exists():
        print(f"缺少清理脚本: {CLEAN}")
        return 2
    if not PY.exists():
        print(f"缺少解释器: {PY}")
        return 2

    xml = build_xml(a.at, a.days, a.rewards_days, a.max_mb)
    print(f"任务 {TASK_NAME}:")
    print(f"  触发: 每天 {a.at}  (无 EndBoundary => 永久)")
    print(f"  动作: {PY} \"{CLEAN}\" --days {a.days} --rewards-days {a.rewards_days}"
          + (f" --max-mb {a.max_mb}" if a.max_mb > 0 else ""))
    print(f"  保留: 异常图 {a.days} 天; 奖励图 {a.rewards_days} 天; outputs/ 按 {a.days} 天")
    print(f"  兜底: captures/ 超过 {a.max_mb} MB 时从最旧开始删" if a.max_mb > 0 else "  兜底: 未设硬上限")
    print("  执行时限: PT1H   StartWhenAvailable: true (关机错过的会补跑)")

    ok, msg = register_task(TASK_NAME, xml)
    print(msg)
    if ok:
        # 旧名 (每月 1 号那版) 如果不卸掉, 会变成"每月又清一次"的僵尸任务
        okL, outL = remove_task(LEGACY_TASK)
        if okL:
            print(f"已卸下旧任务 {LEGACY_TASK} (月度版, 已被每日版取代)")
        else:
            print(f"旧任务 {LEGACY_TASK}: {outL}")
        print("\n装完必看 (看门狗就是 NextRun 为空才暴露的):")
        print("  " + verify_next_run(TASK_NAME))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
