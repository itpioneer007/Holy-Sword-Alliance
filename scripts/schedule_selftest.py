"""预约启动自测 (2026-09-22 新增功能: "人不在, 到点自动开打")。

被测对象: sj_bot/server.py 的 JobScheduler + sj_bot/state_machine.py 的窗口口径。
**不碰真机/不碰 OCR/不碰 5050**, 纯逻辑 + 落盘往返, 秒级跑完。

覆盖:
  A. _parse_when 输入解析   : 定点 HH:MM / 已过顺延明天 / 相对分钟 / 秒 / 小时 / 非法输入
  B. window_state 时段口径  : arena(周六日16-17) / rank_glory(18-20) / rank(避让18-20) / collect
  C. 登记预检               : 当日无可用时段 -> 当场拒绝 (不留"假预约")
  D. 到点触发               : 窗口已开 -> 立即下发; 同类型在跑 -> 视为已满足(不重复启动);
                              其它任务在跑 -> 等它结束; 错过当日 -> 作废
  E. 等窗口                 : 约在窗口前 (如 17:00 约"30s") -> 到点不启动, 等 18:00 开放才启动
                              —— 这就是"提前和程序说好, 6 点自动开打"的核心路径
  F. 落盘/恢复              : 重启后预约仍在 (到点前重启=无缝); 有效期已过的条目启动时丢弃
  G. 参数口径               : 预约路径与手动路径共用 _norm_params (不会绕过 rank 每日 30 把上限)
  H. 时段互斥文案           : _window_error 与历史版本逐字一致 (前端/习惯依赖)
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot import state_machine as sm              # noqa: E402
from sj_bot.botlog import BotLog                     # noqa: E402
from sj_bot.server import (                          # noqa: E402
    JobScheduler, _norm_params, _window_error,
)

PASS = 0
FAIL = 0
# 2026-09-22 是周二 (arena 窗口=周六/日 16:00-17:00, 故周二不开放)
TUE = _dt.datetime(2026, 9, 22, 10, 0, 0)
SAT = _dt.datetime(2026, 9, 26, 10, 0, 0)


def check(label: str, got: object, want: object) -> None:
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def hm(t) -> str:
    """datetime -> 'MM-DD HH:MM' (断言用, 避免秒级噪声)。"""
    return t.strftime("%m-%d %H:%M") if isinstance(t, _dt.datetime) else t


class FakeJobs:
    """替身 JobManager: 只提供调度器需要的两个动作。"""

    def __init__(self) -> None:
        self.cur: str | None = None
        self.cleared = 0

    def status(self) -> dict:
        return {"running": self.cur is not None, "type": self.cur}

    def clear_if_done(self) -> None:
        self.cleared += 1


class Rec:
    """替身下发函数: 记录调用, 可指定返回错误。"""

    def __init__(self, err: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.err = err

    def __call__(self, jtype: str, params: dict):
        self.calls.append((jtype, dict(params)))
        return self.err


def new_sched(tmp: Path, name: str = "schedule.json") -> tuple[JobScheduler, FakeJobs, Rec]:
    jobs, rec = FakeJobs(), Rec()
    return JobScheduler(tmp / name, BotLog(), jobs, rec), jobs, rec


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sj_sched_test_"))

    # ---------------- A. _parse_when 输入解析 ----------------
    print("\n--- A. _parse_when 输入解析 ---")
    fire, label = JobScheduler._parse_when("18:00", TUE)
    check("A1 '18:00' 未到 -> 今天18:00", (hm(fire), "18:00:00" in label),
          ("09-22 18:00", True))
    fire, _ = JobScheduler._parse_when("08:00", TUE)
    check("A2 '08:00' 已过 -> 顺延明天", hm(fire), "09-23 08:00")
    fire, label = JobScheduler._parse_when("90", TUE)
    check("A3 '90' -> 90 分钟后", (hm(fire), "1 小时30 分" in label),
          ("09-22 11:30", True))
    fire, _ = JobScheduler._parse_when("90分钟", TUE)
    check("A4 '90分钟' 与 '90' 同解", hm(fire), "09-22 11:30")
    fire, _ = JobScheduler._parse_when("30s", TUE)
    check("A5 '30s' -> 30 秒后", (hm(fire), fire.second), ("09-22 10:00", 30))
    fire, _ = JobScheduler._parse_when("2小时", TUE)
    check("A6 '2小时' -> +2h", hm(fire), "09-22 12:00")
    fire, _ = JobScheduler._parse_when("+45", TUE)
    check("A7 '+45' 带加号也认", hm(fire), "09-22 10:45")
    for bad in ("", "   ", "abc", "25:00", "18:70", "0", "-5", "18:00xyz"):
        try:
            JobScheduler._parse_when(bad, TUE)
            check(f"A8 非法输入 {bad!r} -> 抛错", "未抛错", "ValueError")
        except ValueError:
            check(f"A8 非法输入 {bad!r} -> 抛错", "ValueError", "ValueError")
    check("A9 '18:00' 跨天顺延边界(正点等于 now 也顺延)",
          hm(JobScheduler._parse_when("10:00", TUE)[0]), "09-23 10:00")

    # ---------------- B. window_state 时段口径 ----------------
    print("\n--- B. window_state 时段口径 ---")
    check("B1 周二 10:00 arena 未开", sm.window_state("arena", TUE)[0], False)
    check("B2 周二 10:00 arena 下次=周六16:00",
          hm(sm.window_state("arena", TUE)[1]), "09-26 16:00")
    check("B3 周六 16:30 arena 开放",
          sm.window_state("arena", SAT.replace(hour=16, minute=30))[0], True)
    check("B4 周二 19:00 rank_glory 开放",
          sm.window_state("rank_glory", TUE.replace(hour=19))[0], True)
    check("B5 周二 10:00 rank_glory 下次=今天18:00",
          hm(sm.window_state("rank_glory", TUE)[1]), "09-22 18:00")
    check("B6 周二 19:00 rank 被荣耀占用",
          sm.window_state("rank", TUE.replace(hour=19))[0], False)
    check("B7 周二 19:00 rank 下次=今天20:00",
          hm(sm.window_state("rank", TUE.replace(hour=19))[1]), "09-22 20:00")
    check("B8 周二 10:00 rank 可用", sm.window_state("rank", TUE)[0], True)
    check("B9 collect 无时段限制", sm.window_state("collect", TUE)[0], True)
    check("B10 next_glory_start 在 18:00 整点 -> 明天",
          hm(sm.next_glory_start(TUE.replace(hour=18))), "09-23 18:00")

    # ---------------- C. 登记预检 ----------------
    print("\n--- C. 登记预检 (不留「假预约」) ---")
    s, jobs, rec = new_sched(tmp, "c.json")
    item, err = s.add("arena", {"rounds": 0}, "16:00", now=TUE)
    check("C1 周二约 arena 当天 -> 当场拒绝", (item is None, "没有可用时段" in (err or "")),
          (True, True))
    item, err = s.add("arena", {"rounds": 0}, "16:00", now=SAT)
    check("C2 周六约 arena 当天 -> 受理", (err, hm(_dt.datetime.fromtimestamp(item["fire_at"]))),
          (None, "09-26 16:00"))
    item, err = s.add("rank_glory", {"rounds": 0}, "18:00", now=TUE)
    check("C3 周二约荣耀 18:00 -> 受理且 planned=18:00",
          (err, hm(_dt.datetime.fromtimestamp(item["planned_at"]))), (None, "09-22 18:00"))
    check("C4 预约仅当日有效: expire=当天 23:59",
          hm(_dt.datetime.fromtimestamp(item["expire_at"])), "09-22 23:59")
    item, err = s.add("rank_glory", {"rounds": 0}, "08:00", now=TUE)
    check("C5 约明天早上 -> planned 落在明天窗口 18:00",
          hm(_dt.datetime.fromtimestamp(item["planned_at"])), "09-23 18:00")
    _, err = s.add("rank_glory", {"rounds": 0}, "", now=TUE)
    check("C6 空时间 -> 拒绝", "请填写预约时间" in (err or ""), True)
    _, err = s.add("rank_glory", {"rounds": 0}, "abc", now=TUE)
    check("C7 乱输入 -> 拒绝", "看不懂的时间格式" in (err or ""), True)
    check("C8 同类型重复登记 -> 覆盖(该类型只留 1 条)", 
          len([i for i in s.items(now=TUE) if i["type"] == "rank_glory"]), 1)
    check("C9 覆盖后取最新一条(前一条是 09-22 18:00, 现应为 09-23 08:00)",
          [(i["params"]["rounds"], hm(_dt.datetime.fromtimestamp(i["fire_at"])))
           for i in s.items(now=TUE) if i["type"] == "rank_glory"], [(0, "09-23 08:00")])
    check("C10 不同类型可并存(各一张卡)", len(s.items(now=TUE)), 2)

    # ---------------- D. 到点触发 / 互斥 / 过期 ----------------
    print("\n--- D. 到点触发与互斥 ---")
    s, jobs, rec = new_sched(tmp, "d.json")
    s.add("rank_glory", {"rounds": 0}, "18:00", now=TUE)
    s._tick(now=TUE.replace(hour=17, minute=59, second=59))
    check("D1 未到点 -> 不下发", rec.calls, [])
    s._tick(now=TUE.replace(hour=18, minute=0, second=1))
    check("D2 到点(窗口已开) -> 下发一次", rec.calls, [("rank_glory", {"rounds": 0})])
    check("D3 触发后预约出队", s.items(now=TUE), [])
    check("D4 触发前先 clear_if_done(清掉已完成槽位)", jobs.cleared, 1)
    check("D5 记录 last.ok 供页面回答「到点到底打了没」", s.last()["rank_glory"]["ok"], True)

    s2, jobs2, rec2 = new_sched(tmp, "d2.json")
    s2.add("rank_glory", {"rounds": 3}, "18:00", now=TUE)
    jobs2.cur = "rank_glory"                     # 已有同类型任务在跑
    s2._tick(now=TUE.replace(hour=18, minute=0, second=1))
    check("D6 同类型在跑 -> 视为已满足, 不重复启动", (rec2.calls, s2.items(now=TUE)), ([], []))
    check("D7 同上 -> last 标记未启动并说明原因",
          "已有同类型任务在运行" in s2.last()["rank_glory"]["msg"], True)

    s3, jobs3, rec3 = new_sched(tmp, "d3.json")
    s3.add("rank_glory", {"rounds": 3}, "18:00", now=TUE)
    jobs3.cur = "collect"                        # 其它任务在跑
    s3._tick(now=TUE.replace(hour=18, minute=0, second=1))
    check("D8 其它任务在跑 -> 先不下发", rec3.calls, [])
    it = s3.items(now=TUE.replace(hour=18, minute=0, second=5))[0]
    check("D9 同上 -> state=waiting + 页面提示等谁", (it["state"], "排行采集" in it["note"]),
          ("waiting", True))
    jobs3.cur = None
    s3._tick(now=TUE.replace(hour=18, minute=1))
    check("D10 前一个任务结束后 -> 自动补启动", rec3.calls, [("rank_glory", {"rounds": 3})])

    s4, jobs4, rec4 = new_sched(tmp, "d4.json")
    s4.add("rank_glory", {"rounds": 0}, "18:00", now=TUE)
    s4._tick(now=_dt.datetime(2026, 9, 23, 0, 30))       # 跨过有效期
    check("D11 错过预约日 -> 作废且不下发", (rec4.calls, s4.items(), s4.last()["rank_glory"]["ok"]),
          ([], [], False))
    check("D12 同上 -> 原因写明「仅当日有效」",
          "预约仅当日有效" in s4.last()["rank_glory"]["msg"], True)

    s5, jobs5, rec5 = new_sched(tmp, "d5.json")
    rec5.err = "已有任务在运行, 请先停止当前任务"
    s5.add("rank_glory", {"rounds": 0}, "18:00", now=TUE)
    s5._tick(now=TUE.replace(hour=18, minute=0, second=1))
    s5._tick(now=TUE.replace(hour=18, minute=0, second=2))   # 再过一秒不许重试
    check("D13 下发失败 -> 只尝试一次, 不重试",
          (len(rec5.calls), s5.items()), (1, []))
    check("D14 下发失败 -> last.ok=False 且文案带错误原因",
          (s5.last()["rank_glory"]["ok"],
           "已有任务在运行" in s5.last()["rank_glory"]["msg"]), (False, True))

    # ---------------- E. 等窗口: 核心路径 ----------------
    print("\n--- E. 等窗口 (提前约好, 到点自动开打) ---")
    s6, jobs6, rec6 = new_sched(tmp, "e.json")
    s6.add("rank_glory", {"rounds": 0}, "30s", now=TUE.replace(hour=17, minute=0))
    at_fire = TUE.replace(hour=17, minute=0, second=31)
    s6._tick(now=at_fire)
    check("E1 到点时窗口未开 -> 不启动, 继续等", rec6.calls, [])
    it = s6.items(now=at_fire)[0]
    check("E2 同上 -> 预计启动=18:00, 剩余等待 >1h",
          (it["window_open"], it["planned_str"], it["wait_sec"] >= 3500),
          (False, "09-22 18:00", True))
    s6._tick(now=TUE.replace(hour=17, minute=59, second=58))
    check("E3 17:59:58 仍未启动", rec6.calls, [])
    s6._tick(now=TUE.replace(hour=18, minute=0, second=2))
    check("E4 18:00:02 窗口一开 -> 自动启动 (无需人工)",
          rec6.calls, [("rank_glory", {"rounds": 0})])

    # ---------------- F. 落盘 / 恢复 ----------------
    print("\n--- F. 落盘与重启恢复 ---")
    s7, jobs7, rec7 = new_sched(tmp, "f.json")
    s7.add("rank_glory", {"rounds": 0}, "18:00", now=TUE)
    check("F1 落盘文件已生成", (tmp / "f.json").exists(), True)
    raw = json.loads((tmp / "f.json").read_text(encoding="utf-8"))
    check("F2 落盘内容含 items.fire_at", "fire_at" in raw["items"]["rank_glory"], True)
    s7b, jobs7b, rec7b = new_sched(tmp, "f.json")        # 模拟控制台重启
    check("F3 重启后预约仍在 (到点前重启=无缝)",
          [hm(_dt.datetime.fromtimestamp(i["fire_at"])) for i in s7b.items(now=TUE)],
          ["09-22 18:00"])
    check("F4 恢复后 last 一并恢复", s7b.last(), {})
    s7b._tick(now=TUE.replace(hour=18, minute=0, second=3))
    check("F5 重启后照样能在到点触发", rec7b.calls, [("rank_glory", {"rounds": 0})])

    # 有效期已过 -> 启动时静默丢弃 (不留僵尸预约)
    bad = tmp / "f2.json"
    bad.write_text(json.dumps({"items": {"rank_glory": {
        "type": "rank_glory", "params": {"rounds": 0}, "when": "18:00",
        "fire_at": TUE.timestamp(), "fire_at_str": "09-22 18:00:00",
        "expire_at": TUE.timestamp() - 10, "planned_at": TUE.timestamp(),
        "state": "pending", "note": ""}}, "last": {}}, ensure_ascii=False), encoding="utf-8")
    s8, jobs8, rec8 = new_sched(tmp, "f2.json")
    check("F6 有效期已过的条目重启时丢弃", s8.items(now=TUE), [])
    bad.write_text("{ 这不是 json", encoding="utf-8")
    s9, _, _ = new_sched(tmp, "f2.json")
    check("F7 文件损坏 -> 不抛异常, 当没有预约", s9.items(now=TUE), [])

    # ---------------- G. 参数口径 ----------------
    print("\n--- G. 参数口径 (预约与手动共用) ---")
    check("G1 rank 40 场 -> 钳制 30", _norm_params("rank", {"rounds": 40})[0], {"rounds": 30})
    check("G2 rank 0 场 -> 拒绝", _norm_params("rank", {"rounds": 0})[1], "排位赛请先选择要打的场数(>=1)")
    check("G3 arena rounds 缺省 -> 0(打到窗口结束)", _norm_params("arena", {})[0], {"rounds": 0})
    check("G4 rank_glory 0 -> 0(挂到窗口结束)", _norm_params("rank_glory", {"rounds": "0"})[0],
          {"rounds": 0})
    check("G5 非数字 -> 拒绝", _norm_params("arena", {"rounds": "abc"})[1], "rounds/pages 需为数字")
    check("G6 collect 页数封顶 400", _norm_params("collect", {"pages": 999})[0], {"pages": 400})

    # ---------------- H. 时段互斥文案 (与历史版本逐字一致) ----------------
    print("\n--- H. 时段互斥文案 ---")
    orig = sm.in_glory_window
    try:
        sm.in_glory_window = lambda now=None: True          # type: ignore[assignment]
        check("H1 荣耀中 -> 日常排位被拒",
              _window_error("rank"),
              "荣耀时刻进行中(18:00-20:00), 系统自动参赛中——日常排位已禁用, "
              "请改用 type=rank_glory (荣耀时刻挂机) 模式")
        check("H2 荣耀中 -> 荣耀挂机放行", _window_error("rank_glory"), None)
        sm.in_glory_window = lambda now=None: False         # type: ignore[assignment]
        check("H3 非荣耀时段 -> 荣耀挂机被拒",
              _window_error("rank_glory"),
              "荣耀时刻仅在每晚 18:00-20:00 开放, 当前未到窗口——日常排位请用 type=rank 模式")
        check("H4 非荣耀时段 -> 日常排位放行", _window_error("rank"), None)
    finally:
        sm.in_glory_window = orig                          # type: ignore[assignment]

    # ---------------- I. 取消 ----------------
    print("\n--- I. 取消预约 ---")
    s10, _, _ = new_sched(tmp, "i.json")
    s10.add("rank_glory", {"rounds": 0}, "18:00", now=TUE)
    s10.add("arena", {"rounds": 0}, "16:00", now=SAT)
    check("I1 两条预约并存", len(s10.items(now=TUE)), 2)
    check("I2 取消单条", s10.cancel("rank_glory"), 1)
    check("I3 剩下的仍是 arena", [i["type"] for i in s10.items(now=TUE)], ["arena"])
    check("I4 取消不存在的预约 -> 0", s10.cancel("rank_glory"), 0)
    check("I5 取消全部", s10.cancel(), 1)
    check("I6 全清后为空", s10.items(now=TUE), [])
    check("I7 取消后落盘同步", json.loads((tmp / "i.json").read_text(encoding="utf-8"))["items"], {})

    # ---------------- J. 预计启动时刻必须是精确边界 ----------------
    # 真机暴露过的缺陷: 15 分钟步进采样把 18:00 报成 18:06, 日志/页面给出错时刻。
    print("\n--- J. 预计启动时刻 = 精确边界 (不是采样格点) ---")
    from sj_bot.server import _window_open_within      # noqa: E402
    t0 = TUE.replace(hour=16, minute=51, second=24)
    end = TUE.replace(hour=23, minute=59, second=59)
    check("J1 16:51 报荣耀 18:00 整点开放", 
          _window_open_within("rank_glory", t0, end).strftime("%H:%M:%S"), "18:00:00")
    check("J2 17:59:58 仍报 18:00:00",
          _window_open_within("rank_glory", t0.replace(hour=17, minute=59, second=58), end).strftime("%H:%M:%S"),
          "18:00:00")
    check("J3 周二 arena 当天无窗口 -> None", _window_open_within("arena", t0, end), None)
    check("J4 周六 15:30 报 arena 16:00 整点",
          _window_open_within("arena", SAT.replace(hour=15, minute=30),
                              SAT.replace(hour=23, minute=59)).strftime("%m-%d %H:%M"), "09-26 16:00")
    check("J5 19:00 报 rank 恢复时刻 20:00 整点",
          _window_open_within("rank", TUE.replace(hour=19), end).strftime("%H:%M:%S"), "20:00:00")
    sJ, jobsJ, recJ = new_sched(tmp, "j.json")
    sJ.add("rank_glory", {"rounds": 0}, "30s", now=TUE.replace(hour=16, minute=51))
    got = sJ.items(now=TUE.replace(hour=16, minute=51, second=31))[0]
    check("J6 items().planned_str 同样是精确 18:00 (与日志一致)",
          (got["planned_str"], got["state"]), ("09-22 18:00", "pending"))

    print(f"\n===== 预约启动自测: {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
