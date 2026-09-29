"""全民争霸打架逻辑自测 (2026-09-17 按用户口述重写单场流程)。

用户口径 (原话要点, 2026-09-17):
  - 全民**没有自身战斗 CD 的硬性要求**; 限制在被挑战者身上 ——
    某人一旦被打就进入「修养中」(约 30s), 期间不可再被挑战;
  - 列表**实时刷新** (谁被打、谁可打都在变), 所以要**快速点击能打的人**;
  - 点「挑战」-> 点「跳过」;
  - 输了 -> 「挑战失败」提示 -> 点「返回」-> 打下一个人;
  - 赢了 -> 加积分弹窗    -> 点「返回」-> 打下一个人。

本自测用 mock 覆盖窗口外无法真机验证的分支 (全部走**真实**的 find_verdict /
find_dismiss / _btn_still_challenge 代码路径, 只 mock OCR 与截图):
  1. 常量: 对手修养中 30s / arena 专有关键词与「返回」收口语义
  2. 失败弹窗 (挑战失败) -> loss 归档 + 只点「返回」
  3. 胜利弹窗 (加积分)   -> win 归档 + 只点「返回」
  4. 失败弹窗同时含"积分"字样 -> 仍判 loss (专有短语优先, 防两边都命中判不出)
  5. 弹窗无关键词 -> errors+1 且不 fatal (走完流程回列表)
  6. 点击前复核翻回「修养中」-> stale_pre: 不点击、不登记攻击、不拉黑
  7. 弹窗未出现(未进战斗页且无弹窗) -> stale: 短等重扫 (老 8s 路径保留)
  8. 主循环: stale_pre 只等 1s 立刻重扫找下一个可打的人 (不是 8s)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot import battler as B  # noqa: E402
from sj_bot import state_machine as sm  # noqa: E402
from sj_bot.db import OpponentDB  # noqa: E402

PASS = 0
FAIL = 0
IMG = np.zeros((900, 1600, 3), dtype=np.uint8)
BTN = (1499, 120)


def check(label: str, got: object, want: object) -> None:
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def popup_ocr(popup_items, skip_seen=True):
    """构造 ROI 敏感的假 OCR: 按钮复核 ROI / 战斗页跳过带 / arena 弹窗区 各给不同词。

    按钮复核 ROI 高 68px (btn y ±34), 据此与 BAND_ROI(145) / ARENA_POPUP_ROI(620) 区分。
    """
    def fake(img, roi=None, scale=1.0):
        if roi is not None and (roi[3] - roi[1]) <= 68:
            return [{"text": "挑战", "cx": float(BTN[0]), "cy": float(BTN[1]),
                     "score": 0.95, "area": 200.0}]
        if roi == B.BAND_ROI:
            if not skip_seen:
                return []
            return [{"text": "跳过", "cx": 1468.0, "cy": 820.0, "score": 0.95, "area": 300.0}]
        if roi == B.ARENA_POPUP_ROI:
            return [dict(it) for it in popup_items]
        return []
    return fake


def make_battler(db: OpponentDB, popup_items, skip_seen=True) -> B.Battler:
    b = B.Battler(mode="arena", db=db, log=lambda lvl, msg: None)
    b._shot_img = lambda: IMG                                          # type: ignore[method-assign]
    b._save_anomaly = lambda tag: None                                 # type: ignore[method-assign]
    b._progress = lambda *a, **k: None                                 # type: ignore[method-assign]
    b._is_on_list = lambda img: True                                   # type: ignore[method-assign]
    b._ocr_texts = popup_ocr(popup_items, skip_seen)                   # type: ignore[method-assign]
    # 单次求值: 让 find_skip / find_verdict / find_dismiss 走真实实现
    b._wait_until = lambda fn, *a, **k: fn()                            # type: ignore[method-assign]
    return b


def run_one(popup_items, dbname, name="9区 试甲", skip_seen=True):
    """跑一场完整 arena 战斗, 返回 (status, taps, battler, db)。"""
    tmp = Path(tempfile.mkdtemp(prefix="sj_bot_arena_fight_"))
    db = OpponentDB(tmp / dbname)
    b = make_battler(db, popup_items, skip_seen)
    taps: list[tuple] = []
    b._tap = lambda x, y: taps.append((x, y))                          # type: ignore[method-assign]
    b.interval = sm.ARENA_INTERVAL_SEC
    st = b._do_one_battle(name, BTN)
    return st, taps, b, db


def main() -> None:
    global PASS, FAIL
    saved_skip_sec = B.ARENA_SKIP_AFTER_SEC
    B.ARENA_SKIP_AFTER_SEC = 0     # 跳过"进战斗页等 10s", 自测无需真等

    # ---------------- 1. 常量语义 ----------------
    check("对手修养中窗口 = 30s", sm.ATTACK_COOLDOWN_SEC, 30)
    check("arena 无自身 CD (单场间隔 0)", sm.ARENA_INTERVAL_SEC, 0)
    check("失败弹窗专有短语", B.ARENA_LOSS_HINTS, ("挑战失败",))
    check("收口按钮以「返回」优先", B.ARENA_DISMISS_HINTS[0], "返回")
    check("胜利词含积分弹窗特征", "积分" in B.ARENA_WIN_HINTS, True)
    check("arena 弹窗 ROI 在结算区内", B.ARENA_POPUP_ROI[1] >= 180, True)

    LOSS_POPUP = [
        {"text": "挑战失败", "cx": 800.0, "cy": 450.0, "score": 0.95, "area": 420.0},
        {"text": "返回", "cx": 800.0, "cy": 560.0, "score": 0.95, "area": 300.0},
    ]
    WIN_POPUP = [
        {"text": "挑战胜利", "cx": 800.0, "cy": 430.0, "score": 0.95, "area": 420.0},
        {"text": "+80积分", "cx": 800.0, "cy": 472.0, "score": 0.90, "area": 300.0},
        {"text": "返回", "cx": 800.0, "cy": 560.0, "score": 0.95, "area": 300.0},
    ]
    AMBIG_POPUP = [
        {"text": "挑战失败", "cx": 800.0, "cy": 450.0, "score": 0.95, "area": 420.0},
        {"text": "本场积分 +0", "cx": 800.0, "cy": 500.0, "score": 0.90, "area": 300.0},
        {"text": "返回", "cx": 800.0, "cy": 560.0, "score": 0.95, "area": 300.0},
    ]
    BLANK_POPUP = [
        {"text": "请稍候", "cx": 800.0, "cy": 450.0, "score": 0.90, "area": 300.0},
    ]

    # ---------------- 2. 失败: 挑战失败 -> loss + 返回 ----------------
    # 点击序列 = [点挑战(开战), 点跳过, 点返回] —— arena 单场只有这三个点击
    TAP_SEQ = [(float(BTN[0]), float(BTN[1])), (1468.0, 820.0), (800.0, 560.0)]
    st, taps, b, db = run_one(LOSS_POPUP, "loss.db")
    check("挑战失败弹窗 -> ok", st, "ok")
    check("失败 记 losses=1", b.losses, 1)
    check("失败 记 wins=0", b.wins, 0)
    check("失败 记 1 场", b.fought, 1)
    check("失败 点击序列 = 挑战/跳过/返回", taps, TAP_SEQ)
    check("失败 首败不降级 (verdict 仍 unknown, 连败2次才入名单)",
          db.get("9区 试甲")["verdict"], "unknown")
    check("失败 已登记对手修养中",
          db.get("9区 试甲")["last_attacked_at"] is not None, True)
    db.close()

    # ---------------- 3. 胜利: 加积分弹窗 -> win + 返回 ----------------
    st, taps, b, db = run_one(WIN_POPUP, "win.db", "9区 试乙")
    check("加积分弹窗 -> ok", st, "ok")
    check("胜利 记 wins=1", b.wins, 1)
    check("胜利 记 losses=0", b.losses, 0)
    check("胜利 点击序列 = 挑战/跳过/返回", taps, TAP_SEQ)
    check("胜利 档案 verdict=beat", db.get("9区 试乙")["verdict"], "beat")
    db.close()

    # ---------------- 4. 失败弹窗含"积分" -> 仍判 loss ----------------
    st, taps, b, db = run_one(AMBIG_POPUP, "ambig.db", "9区 试丙")
    check("失败/积分同现 -> 判 loss", (b.losses, b.wins), (1, 0))
    check("失败/积分同现 -> 仍点返回", taps, TAP_SEQ)
    db.close()

    # ---------------- 5. 弹窗无关键词 -> errors+1, 不 fatal ----------------
    st, taps, b, db = run_one(BLANK_POPUP, "blank.db", "9区 试丁")
    check("弹窗无关键词 -> 不 fatal", st, "ok")
    check("弹窗无关键词 -> errors=1", b.errors, 1)
    check("弹窗无关键词 -> 找不到返回则不乱点", taps, TAP_SEQ[:2])
    check("弹窗无关键词 -> 不写档案胜负",
          (db.get("9区 试丁")["wins"], db.get("9区 试丁")["losses"]), (0, 0))
    db.close()

    # ---------------- 6. 点击前复核翻回「修养中」-> stale_pre ----------------
    tmp = Path(tempfile.mkdtemp(prefix="sj_bot_arena_fight_"))
    db = OpponentDB(tmp / "pre.db")
    bp = B.Battler(mode="arena", db=db, log=lambda lvl, msg: None)
    bp._shot_img = lambda: IMG                                          # type: ignore[method-assign]
    bp._save_anomaly = lambda tag: None                                 # type: ignore[method-assign]
    bp._progress = lambda *a, **k: None                                 # type: ignore[method-assign]
    bp._is_on_list = lambda img: True                                   # type: ignore[method-assign]
    bp._wait_until = lambda fn, *a, **k: fn()                           # type: ignore[method-assign]
    pre_taps: list[tuple] = []
    bp._tap = lambda x, y: pre_taps.append((x, y))                      # type: ignore[method-assign]

    def pre_ocr(img, roi=None, scale=1.0):
        if roi is not None and (roi[3] - roi[1]) <= 68:
            return [{"text": "修养中[12秒]", "cx": float(BTN[0]), "cy": float(BTN[1]),
                     "score": 0.92, "area": 260.0}]
        return []

    bp._ocr_texts = pre_ocr                                             # type: ignore[method-assign]
    check("点击前复核翻回修养中 -> stale_pre", bp._do_one_battle("9区 试戊", BTN), "stale_pre")
    check("stale_pre 不点击", pre_taps, [])
    check("stale_pre 不登记攻击 (免点免等, 不留残留)",
          db.get("9区 试戊")["last_attacked_at"], None)
    check("stale_pre 不计异常", (bp.errors, bp.fought), (0, 0))
    db.close()

    # ---------------- 7. 未进战斗页且无弹窗 -> stale ----------------
    st, taps, b, db = run_one([], "stale.db", "9区 试己", skip_seen=False)
    check("未进战斗页无弹窗 -> stale", st, "stale")
    check("stale 只点了挑战(未进战斗页, 无误点)", taps, [TAP_SEQ[0]])
    check("stale 不写档案", b.fought, 0)
    db.close()

    # ---------------- 8. 主循环: stale_pre 只等 1s 立刻重扫 ----------------
    tmp = Path(tempfile.mkdtemp(prefix="sj_bot_arena_fight_"))
    db = OpponentDB(tmp / "loop.db")
    bl = B.Battler(mode="arena", db=db, log=lambda lvl, msg: None)
    bl._shot_img = lambda: IMG                                          # type: ignore[method-assign]
    bl._progress = lambda *a, **k: None                                 # type: ignore[method-assign]
    bl._is_on_list = lambda img: True                                   # type: ignore[method-assign]
    bl._is_in_battle = lambda img: False                                # type: ignore[method-assign]
    bl._enter_pvp_hub_retry = lambda tag, **k: True                     # type: ignore[method-assign]
    bl._pick_target = lambda img: ("9区 试庚", BTN)                      # type: ignore[method-assign]
    bl._do_one_battle = lambda n, btn: "stale_pre"                      # type: ignore[method-assign]
    sleeps: list[float] = []
    bl._sleep_stop = lambda sec: sleeps.append(sec)                     # type: ignore[method-assign]
    old_in_window = B.in_window
    B.in_window = lambda now=None: True                                 # type: ignore[assignment]
    res = bl.run()
    B.in_window = old_in_window
    check("stale_pre 连击达上限 -> 收尾 stale_list", res["reason"], "stale_list")
    check("stale_pre 重扫等待 1.0s (非 8s)", sleeps, [B.STALE_PRE_RETRY_SEC] * 4)
    check("STALE_PRE_RETRY_SEC 小于老 8s 路径", B.STALE_PRE_RETRY_SEC < B.STALE_RETRY_SEC, True)
    check("arena 无人可打重扫间隔 5s", B.ARENA_EMPTY_RETRY, 5)
    db.close()

    B.ARENA_SKIP_AFTER_SEC = saved_skip_sec
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
