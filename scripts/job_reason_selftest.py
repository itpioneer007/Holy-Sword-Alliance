# -*- coding: utf-8 -*-
"""任务终止原因规范化 + "上次任务"摘要 自测 (2026-09-22)。

背景 (用户需求): "终止原因也要一眼就能看到"。
引擎的 exit_status 是内部分类码 (battler.py 各收尾分支), 直接给用户看就是一句天书;
server.JOB_REASON 是唯一一份"人话版"映射, /api/status.last_job 把它送到页面顶部常驻条。

本自测守两条线:
  1. 映射行为正确: 已知码 -> 中文/异常标记/建议; 未知码原样回显且标异常 (绝不静默吞掉);
  2. **覆盖性**: battler.py 里**所有**会被写进 reason 的分类码都必须在 JOB_REASON 里有条目
     —— 否则引擎新增一个收尾分支而词表没跟上, 页面就会显示裸英文码 (本次就是为了防这个)。

跑法: <venv python> scripts/job_reason_selftest.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sj_bot.server as S            # noqa: E402

RESULTS: list[bool] = []


def check(name: str, got, expect) -> None:
    ok = got == expect
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got} (期望 {expect})")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATTLER = os.path.join(ROOT, "sj_bot", "battler.py")
WEB = os.path.join(ROOT, "web", "index.html")

# ------------------------------------------------ A. reason_info 行为
print("=== A. reason_info: 已知 / 未知 / 空 ===")
r = S.reason_info("finished")
check("A1 正常收尾 -> abnormal=False", r["abnormal"], False)
check("A1 有中文说明", bool(r["cn"]) and r["cn"] != "finished", True)
check("A1 known=True", r["known"], True)

r = S.reason_info("selfheal_stall")
check("A2 窗口内无进展 -> abnormal=True", r["abnormal"], True)
check("A2 带处理建议", bool(r["hint"]), True)

r = S.reason_info("window_closed")
check("A3 窗口关闭算正常收尾", r["abnormal"], False)

r = S.reason_info("no_battle_ui")
check("A4 no_battle_ui 建议里点名内存根因", "内存" in r["hint"], True)

r = S.reason_info("我编的码")
check("A5 未知码原样回显 (不吞掉)", r["cn"], "我编的码")
check("A5 未知码标为异常", r["abnormal"], True)
check("A5 known=False 可被观测", r["known"], False)

r = S.reason_info(None)
check("A6 空码 -> 未知原因 + 异常", (r["cn"], r["abnormal"]), ("未知原因", True))

# ------------------------------------------------ B. enrich_result
print("\n=== B. enrich_result: 给结果补派生字段 ===")
raw = {"mode": "rank_glory", "reason": "selfheal_stall", "fought": 19}
out = S.enrich_result(raw)
check("B1 补了 reason_cn", out["reason_cn"], S.JOB_REASON["selfheal_stall"][0])
check("B2 补了 abnormal", out["abnormal"], True)
check("B3 不动引擎原字段", (out["fought"], out["mode"]), (19, "rank_glory"))
check("B4 原 dict 未被就地改动", "reason_cn" in raw, False)
check("B5 None 安全", S.enrich_result(None), None)
check("B6 非 dict 原样返回", S.enrich_result("x"), "x")

# ------------------------------------------------ C. 覆盖性: 引擎所有收尾码都在表里
print("\n=== C. 覆盖性: battler.py 的收尾码必须全部登记 ===")
src = open(BATTLER, encoding="utf-8").read()
codes: set[str] = set()
# 逐行抓, 避免跨行误配
for line in src.splitlines():
    if ("exit_status" in line or "flow_exit" in line or "_summary(" in line
            or "_stop_reason(" in line):
        for m in re.finditer(r'"(no_[a-z]+|[a-z][a-z_0-9]{2,})"', line):
            codes.add(m.group(1))
# 闸门内部合成码 (由 _selfheal_ok 写入, 不出现在 exit_status 字面量里)
codes |= {"window_closed", "selfheal_stall", "selfheal_cap"}
# 结算超限走 "no_" + stage 动态拼装, 三个取值必须都在表里
codes |= {f"no_{s}" for s in ("reward", "end", "back")}
# 这些是**比较值/页面名/动态前缀**, 不是收尾码, 明确排除 (避免误报)
NOT_CODES = {"stop", "recovered", "stopped", "battle", "reward", "end",
             "back", "hub", "main_city", "unknown", "ok", "fatal",
             "stale", "stale_pre", "blocked", "scan", "settle", "starting",
             "no_"}   # "no_" 是 f 拼装前缀, 三个具体取值已显式补入 codes
declared = {c for c in codes if c not in NOT_CODES}
# 动态 f-string 拼装的收尾码 (扫描器抓不到字面量, 必须显式登记, 否则会静默漏)
DYNAMIC = ["antibot_giveup", "antibot_retry", "antibot_no_llm",   # f"antibot_{solved}"
           "no_reward", "no_end", "no_back"]                      # "no_" + stage
declared |= set(DYNAMIC)

missing = sorted(c for c in declared if c not in S.JOB_REASON)
check(f"C1 引擎收尾码全部有中文说明 (扫描到 {len(declared)} 个)", missing, [])
print(f"      扫描到的码: {sorted(declared)}")

# 反向: 表里有、引擎已不产生的码 (只提示, 不算失败 —— 历史任务记录仍可能带旧码)
_extra = sorted(c for c in S.JOB_REASON
                if c not in declared and c not in ("finished", "stopped",
                                                   "not_in_window", "not_glory_window",
                                                   "nav_failed", "bad_rounds", "glory_done"))
if _extra:
    print(f"      (提示) 表中未被当前源码引用的码 (历史记录仍可能用): {_extra}")

# ------------------------------------------------ D. 前端兜底词表要与服务端同步
print("\n=== D. 前端兜底词表同步 (新增码不得漏) ===")
web = open(WEB, encoding="utf-8").read()
for code in ("window_closed", "selfheal_stall", "selfheal_cap", "not_list"):
    check(f"D1 前端含新码 {code}", code in web, True)
check("D2 前端优先用服务端 reason_cn", "r.reason_cn ||" in web, True)
check("D3 前端有常驻条 jobStrip", 'id="jobStrip"' in web, True)

# ------------------------------------------------ E. _last_job_summary
print("\n=== E. _last_job_summary: 优先内存槽位, 回落落盘历史 ===")


class _StubHist:
    def __init__(self, items):
        self._items = items

    def recent(self, n):
        return self._items[:n]


class _StubJobs:
    def __init__(self, st, items):
        self._st = st
        self._history = _StubHist(items)

    def status(self):
        return self._st


_HIST = [{"ts": "2026-09-22 18:42:14", "type": "rank_glory", "state": "done",
          "error": None, "result": {"reason": "no_battle_ui", "fought": 19}}]

_saved = S.JOBS
try:
    # E1: 刚结束 (槽位保留) -> 用槽位里的那次
    S.JOBS = _StubJobs({"running": False, "state": "done", "type": "rank_glory",
                        "started_at": "2026-09-22 18:00:00", "error": None,
                        "result": {"reason": "selfheal_stall", "fought": 19}}, _HIST)
    s = S._last_job_summary()
    check("E1 用内存槽位 (reason 取最新那次)", s["result"]["reason"], "selfheal_stall")
    check("E1 带中文原因", s["result"]["reason_cn"], S.JOB_REASON["selfheal_stall"][0])
    check("E1 带类型中文", s["type_cn"], "荣耀时刻挂机")
    check("E1 带异常标记 (页面据此标红)", s["result"]["abnormal"], True)

    # E2: 正在跑 (槽位被占用) -> 回落历史里最近一条"已结束"
    S.JOBS = _StubJobs({"running": True, "state": "running", "type": "arena",
                        "result": None, "error": None}, _HIST)
    s = S._last_job_summary()
    check("E2 运行中 -> 回落历史", s["result"]["reason"], "no_battle_ui")
    check("E2 时间取历史 ts", s["ts"], "2026-09-22 18:42:14")

    # E3: 无槽位无历史 -> None (页面隐藏该条)
    S.JOBS = _StubJobs({"running": False}, [])
    check("E3 都没有 -> None", S._last_job_summary(), None)
finally:
    S.JOBS = _saved

print(f"\n任务终止原因自测: {sum(RESULTS)} 通过 / {len(RESULTS) - sum(RESULTS)} 失败")
sys.exit(0 if all(RESULTS) else 1)
