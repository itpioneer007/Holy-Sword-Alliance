"""全民争霸 2026-09-11 新规自测: 行动力机制 (旧 30s 单场 CD 作废)。

背景: 游戏面板改为「行动力」机制 —— 每次挑战消耗 1 点, 初始 10 点,
每分钟恢复 1 点, 上限 20 点。即: 有行动力可连打(单场间隔 0), 行动力耗尽后
"一分钟一把"。已实测: 行动力数字 OCR 不可靠(顶栏"当前行动力："后的数字会丢),
因此不能读数值做判断, 只能用"点挑战后是否进入战斗页"这一行为信号识别。

本自测用 mock 覆盖无法在窗口外真机验证的关键分支:
  1. 常量: 单场间隔 0 / 恢复 60s / 每胜 80 分 / 窗口 16:00-17:00
  2. _looks_like_stamina: 弹窗文案分类
  3. _do_one_battle 三态返回:
     blocked = 未进战斗页且仍停在列表(行动力不足)  -> 不留对手修养中残留
     fatal   = 既不在列表也识别不到战斗页(界面未知)
     ok      = 正常打完, 且此刻才登记对手「修养中」(30s, 2026-09-17 修订)
  4. _close_popup: 只点取消/关闭, 绝不点"购买"(防扣钻石)
  5. run() 主循环: blocked -> 等 60s 重试, stamina_waits 计数, 成功一战清零
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot import battler as B  # noqa: E402
from sj_bot.db import OpponentDB  # noqa: E402
from sj_bot import state_machine as sm  # noqa: E402

PASS = 0
FAIL = 0
FAKE_IMG = np.zeros((900, 1600, 3), dtype=np.uint8)


def check(label: str, got: object, want: object) -> None:
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def make_battler(db: OpponentDB, mode: str = "arena") -> B.Battler:
    logs: list[str] = []
    b = B.Battler(mode=mode, db=db, log=lambda lvl, msg: logs.append(f"{lvl}:{msg}"))
    b.logs = logs  # type: ignore[attr-defined]
    b._shot_img = lambda: FAKE_IMG            # type: ignore[method-assign]
    b._tap = lambda x, y: None                # type: ignore[method-assign]
    b._save_anomaly = lambda tag: None        # type: ignore[method-assign]
    b._progress = lambda *a, **k: None        # type: ignore[method-assign]
    return b


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="sj_bot_stamina_")

    # ---------------- 1. 常量 ----------------
    check("单场间隔 0", sm.ARENA_INTERVAL_SEC, 0)
    check("行动力耗尽 60s 一场", sm.STAMINA_RECOVER_SEC, 60)
    check("每胜 80 分", sm.POINTS_PER_WIN, 80)
    check("窗口 16:00 起", sm.OPEN_START_HOUR, 16)
    check("MAX_BLOCK_STREAK=5", B.MAX_BLOCK_STREAK, 5)
    db0 = OpponentDB(Path(tmp) / "c.db")
    b0 = make_battler(db0, "arena")
    check("arena 单场间隔 = 0", b0.interval, 0)
    b1 = make_battler(db0, "rank")
    check("rank 单场间隔仍 30s", b1.interval, 30)

    # ---------------- 2. 弹窗文案分类 ----------------
    check("行动力不足弹窗 -> 识别", b0._looks_like_stamina("行动力不足，是否购买行动力？"), True)
    check("体力不足弹窗 -> 识别", b0._looks_like_stamina("体力不足"), True)
    check("活动未开启 -> 不算行动力", b0._looks_like_stamina("活动尚未开启，请于活动时间参与"), False)
    check("空文本 -> False", b0._looks_like_stamina(""), False)

    # ---------------- 3. _close_popup 绝不点"购买" ----------------
    for h in B.POPUP_CLOSE_HINTS:
        check(f"关闭词表不含'购买'相关({h})", ("购买" in h) or ("确定" in h), False)
    hit_cancel = {"text": "取消", "cx": 700.0, "cy": 500.0, "score": 0.9, "area": 100.0}
    hit_buy = {"text": "购买行动力", "cx": 900.0, "cy": 500.0, "score": 0.99, "area": 500.0}
    taps: list[tuple] = []
    b2 = make_battler(db0)
    b2._ocr_texts = lambda *a, **k: [dict(hit_buy), dict(hit_cancel)]  # type: ignore[method-assign]
    b2._tap = lambda x, y: taps.append((x, y))                         # type: ignore[method-assign]
    ok_close = b2._close_popup(FAKE_IMG)
    check("弹窗可关闭", ok_close, True)
    check("只点到 取消(700,500), 未点 购买(900,500)", taps, [(700.0, 500.0)])

    # ---------------- 4. _do_one_battle 三态 ----------------
    # 4a. blocked: 等不到"跳过", 但仍停在列表 + 行动力不足弹窗
    db_a = OpponentDB(Path(tmp) / "a.db")
    ba = make_battler(db_a)
    ba._wait_until = lambda *a, **k: None                              # type: ignore[method-assign]
    ba._is_on_list = lambda img: True                                  # type: ignore[method-assign]
    popup = {"text": "行动力不足", "cx": 800.0, "cy": 400.0, "score": 0.9, "area": 200.0}
    ba._ocr_texts = lambda *a, **k: [dict(popup)]                      # type: ignore[method-assign]
    st = ba._do_one_battle("9区 测试甲", (1499, 120))
    check("未进战斗页且停在列表 -> blocked", st, "blocked")
    check("blocked 不留 10 分钟冷却残留",
          db_a.get("9区 测试甲")["last_attacked_at"], None)
    check("blocked 不计异常 errors=0", ba.errors, 0)
    check("blocked 不计场次 fought=0", ba.fought, 0)

    # 4b. fatal: 既不在列表, 也识别不到任何弹窗文字
    db_b = OpponentDB(Path(tmp) / "b.db")
    bb = make_battler(db_b)
    bb._wait_until = lambda *a, **k: None                              # type: ignore[method-assign]
    bb._is_on_list = lambda img: False                                 # type: ignore[method-assign]
    bb._ocr_texts = lambda *a, **k: []                                 # type: ignore[method-assign]
    check("界面未知 -> fatal", bb._do_one_battle("9区 测试乙", (1499, 120)), "fatal")

    # 4c. ok: 完整打完一场 -> 登记冷却 + 胜场计数
    db_c = OpponentDB(Path(tmp) / "c2.db")
    bc = make_battler(db_c)
    skip_hit = {"text": "跳过", "cx": 1468.0, "cy": 820.0, "score": 0.9, "area": 300.0,
                "match": "跳过"}
    dismiss_hit = {"text": "确定", "cx": 800.0, "cy": 600.0, "score": 0.9, "area": 300.0,
                   "match": "确定"}
    seq = [skip_hit, "win", dismiss_hit, True]
    bc._wait_until = lambda *a, **k: seq.pop(0)                         # type: ignore[method-assign]
    bc._is_on_list = lambda img: True                                   # type: ignore[method-assign]
    bc.interval = 0                                                     # 新规: 无间隔
    check("正常一场 -> ok", bc._do_one_battle("9区 测试丙", (1499, 120)), "ok")
    check("ok 记 1 胜", bc.wins, 1)
    check("ok 记 1 场", bc.fought, 1)
    check("战斗页确认后才登记对手修养中",
          db_c.get("9区 测试丙")["last_attacked_at"] is not None, True)

    # ---------------- 5. run() 主循环: blocked -> 等 60s 重试 ----------------
    db_d = OpponentDB(Path(tmp) / "d.db")
    bd = make_battler(db_d)
    bd.stage_shots = False
    B.in_window = lambda now=None: True            # 强制在窗口内(窗口判定另有 rules_selftest)
    bd._enter_pvp_hub_retry = lambda tag, **k: True  # type: ignore[method-assign]
    bd._shot_img = lambda: FAKE_IMG                # type: ignore[method-assign]
    bd._is_on_list = lambda img: True              # type: ignore[method-assign]
    blocked_calls = {"n": 0}

    def fake_battle(name, btn):
        blocked_calls["n"] += 1
        return "blocked"          # 永远被行动力挡住

    bd._do_one_battle = fake_battle                # type: ignore[method-assign]
    bd._pick_target = lambda img: ("9区 测试丁", (1499, 120))  # type: ignore[method-assign]
    sleeps: list[float] = []
    bd._sleep_stop = lambda sec: sleeps.append(sec)  # type: ignore[method-assign]

    def fake_wait_until(fn, timeout, interval, desc, tick=None):
        return fn() if not callable(fn) else fn()

    # 让每次循环都"扫描成功", 但第 5 次 blocked 触发 MAX_BLOCK_STREAK 收尾
    res = bd.run()
    check("连续 blocked 达上限后收尾", res["reason"], "no_stamina")
    check("stamina_waits 计数 = 5", res["stamina_waits"], 5)
    # 第 1~4 次 blocked 各等 60s 重试; 第 5 次已达上限 -> 直接终止不再等
    check("前 4 次 blocked 各等 60s 后重试", sleeps, [60] * 4)
    check("blocked 不产生战败/异常", (res["fought"], res["errors"]), (0, 0))

    db0.close()
    db_a.close()
    db_b.close()
    db_c.close()
    db_d.close()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
