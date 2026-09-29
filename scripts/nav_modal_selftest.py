# -*- coding: utf-8 -*-
"""导航「全屏模态盖住主城 -> 按一次返回键清模态」自测 (2026-09-29)。

回归背景 (真机实录: 09-29 18:18~18:23 荣耀任务以 session_nav 异常终止)
    第 10 局结算时游戏弹出「token失效, 请重新登录」(真实会话过期) -> 重新登录
    -> 游戏又弹出「幸运大转盘」全屏活动页。该页**四周仍露出主城建筑文字**
    (荣誉殿堂 / 铁匠铺 / 英雄城堡 ...), 于是 `_is_in_main_city` 的全图回退稳定命中
    ≥2 个宽词表词 => 判成 main_city; 而入口「王者之巅」正好被模态压住 =>
    同一帧里"是主城"却"没有入口"。
    旧行为在此直接 `return False` => 3 轮重试各自截图都落在同一处 =>
    "导航连续 3 次失败" 收尾, 白丢 5 分钟窗口。
    已知同型: 「背包/宝石合成」面板 (09-29 17:35 实录, 也是导航失败 3 连)。

本自测锁死的契约
  A 段 (桩化流程, 不依赖 adb/OCR)
    A1 主城判定成立 + 锚点两帧都找不到 -> **按且只按一次**返回键; 按完锚点出现
       -> 继续正常导航并返回 True
    A2 按完锚点仍找不到 -> 返回 False (仍只按一次, 不连按)
    A3 本轮已按过 (self._nav_modal_back) -> 不再按, 直接 False
    A4 按完返回键发现游戏已不在前台 -> 转 _escape_relaunch_game, 不继续瞎按
    A5 干净主城 (首帧锚点就在) -> **一次返回键都不按** (新分支不误伤正常流程)
  B 段 (真实帧前置条件, 需 OCR, ~1 分钟; --no-real 可跳过)
    用固化的三张真机帧证明: 两类模态帧确实被判成主城且锚点缺失 (= 分支的触发条件),
    干净主城帧则锚点可见 (= 不会触发), 从而把"为什么需要这条分支"钉在真实数据上。

样本位置: assets/modal_ref/*.jpg
  promo_wheel.jpg  登录后「幸运大转盘」全屏活动页
  bag_panel.jpg    「背包/宝石合成」全屏面板
  city_clean.jpg   干净主城 (对照, 锚点可见)
⚠️ 样本放 assets/ 而不是 captures/ —— captures 有月度清理, 放那里下次就会丢。
"""
import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import cv2          # noqa: E402
import numpy as np  # noqa: E402

from sj_bot.battler import Battler  # noqa: E402

REF = os.path.join(_ROOT, "assets", "modal_ref")


# ============================================================ A 段: 桩化流程
def make_battler(frames, health="ok", relaunch=None):
    """构造一个只跑 _enter_pvp_hub 的 Battler 桩。

    frames: `_shot_img()` 依次返回的帧标记 (最后一个会一直重复)。
            只有 "modal" 会被判成主城, 只有 "city" 上找得到「王者之巅」锚点。
    """
    b = object.__new__(Battler)
    b.errors = 0
    b._nav_page = None
    b._at_hub_confident = False
    b._nav_back_tried = False
    b._nav_modal_back = False
    b.stop_evt = threading.Event()
    b.log = lambda lvl, msg: None
    b.anomalies = []
    b.back_count = 0

    seq = {"i": 0}

    def shot():
        i = seq["i"]
        seq["i"] += 1
        return frames[min(i, len(frames) - 1)]

    b._shot_img = shot

    def press_back():
        b.back_count += 1
        return True

    b._press_back = press_back
    b._sleep_stop = lambda s: None
    b._tap = lambda *a, **k: None
    b._wait_hint = lambda *a, **k: True
    b._tap_rank_entry = lambda img: None
    b._save_anomaly = lambda tag: b.anomalies.append(tag)
    b._classify_page = lambda img=None: "hub"      # 落点断言: 点完入口即主页

    # 页面判据全部桩化。帧标记语义:
    #   "modal" = 被判成主城, 但「王者之巅」锚点被模态压住 (缺锚点)
    #   "city"  = 真主城, 被判成主城**且**锚点可见
    b._is_in_battle = lambda img=None: False
    b._is_rank_hub = lambda img=None, fast=False: False
    b._is_pvp_submenu = lambda img=None: False
    b._is_in_main_city = lambda img=None: img in ("modal", "city")
    b._is_city = lambda img=None: img == "city"
    b._is_session_expired = lambda img=None: False
    b._is_login_screen = lambda img=None: False

    def find_hint(img, hints, roi=None, scale=1.0):
        if "王者之巅" in hints:
            return {"cx": 500, "cy": 600} if img == "city" else None
        return None

    b._find_hint = find_hint
    b._check_game_health = lambda: health
    b._escape_relaunch_game = lambda tag: (relaunch.append(tag) if relaunch is not None else None,
                                          "main_city")[1]
    return b


RESULTS = []


def expect(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return cond


def section_a():
    print("=== A 段: 桩化流程 (模态遮挡 -> 按一次返回键) ===")

    # A1: 模态两帧无锚点 -> 按一次 back -> 第三帧锚点出现 -> 导航成功
    b = make_battler(["modal", "modal", "city"])
    ok = b._enter_pvp_hub("rank")
    expect("A1 模态遮挡: 按一次返回键后锚点出现 -> 导航成功",
           ok is True and b.back_count == 1,
           f"ok={ok} back={b.back_count}")
    expect("A1 留图: 已存 no_wangzhe_anchor_modal (便于事后看图确认是哪种模态)",
           "no_wangzhe_anchor_modal" in b.anomalies, str(b.anomalies))

    # A2: 按完仍无锚点 -> 失败, 但**只按一次** (绝不连按 -> 会把游戏按退出到桌面)
    b = make_battler(["modal"])
    ok = b._enter_pvp_hub("rank")
    expect("A2 按完仍无锚点 -> 返回 False, 且只按了一次返回键",
           ok is False and b.back_count == 1,
           f"ok={ok} back={b.back_count}")
    expect("A2 两种情况都留图 (modal / 普通) 便于区分",
           {"no_wangzhe_anchor_modal", "no_wangzhe_anchor"} <= set(b.anomalies),
           str(b.anomalies))

    # A3: 本轮已按过 -> 不再按 (防一个导航轮次里反复按 back)
    b = make_battler(["modal"])
    b._nav_modal_back = True
    ok = b._enter_pvp_hub("rank")
    expect("A3 本轮已按过 -> 不再按返回键, 直接失败",
           ok is False and b.back_count == 0,
           f"ok={ok} back={b.back_count}")

    # A4: 按完发现游戏不在前台 -> 转拉起游戏 (桌面/黑屏上按 back 永远无效)
    rl = []
    b = make_battler(["modal", "modal", "city"], health="gone", relaunch=rl)
    ok = b._enter_pvp_hub("rank")
    expect("A4 按完返回键发现游戏已不在前台 -> 走拉起游戏",
           rl == ["nav_modal"], f"relaunch={rl}")
    expect("A4 拉起后锚点出现 -> 仍能导航成功", ok is True, f"ok={ok}")

    # A5: 干净主城 (首帧就有锚点) -> 一次 back 都不按
    b = make_battler(["city"])
    ok = b._enter_pvp_hub("rank")
    expect("A5 干净主城: 一次返回键都不按, 正常导航成功",
           ok is True and b.back_count == 0,
           f"ok={ok} back={b.back_count}")


# ============================================================ B 段: 真实帧
def _imread(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def section_b():
    print("\n=== B 段: 真实帧前置条件 (为什么需要这条分支) ===")
    b = Battler(mode="rank", log=lambda lvl, msg: None)
    cases = [
        # (文件, 期望被判成主城, 期望锚点可见, 说明)
        ("promo_wheel.jpg", True, False, "登录后「幸运大转盘」活动页"),
        ("bag_panel.jpg", True, False, "「背包/宝石合成」面板"),
        ("city_clean.jpg", True, True, "干净主城 (对照)"),
    ]
    for name, want_city, want_anchor, desc in cases:
        path = os.path.join(REF, name)
        if not os.path.exists(path):
            expect(f"B 样本存在: {name}", False, "文件缺失 (应从 assets/modal_ref 取出)")
            continue
        img = _imread(path)
        if img is None:
            expect(f"B 样本可读: {name}", False, "解码失败")
            continue
        t0 = time.time()
        mc = b._is_in_main_city(img)
        ac = b._is_city(img)
        dt = time.time() - t0
        tag = f"{desc} ({name})"
        expect(f"B {tag}: _is_in_main_city={want_city}", mc is want_city, f"实得 {mc} / {dt:.1f}s")
        expect(f"B {tag}: _is_city(锚点可见)={want_anchor}", ac is want_anchor, f"实得 {ac}")
        if want_city and not want_anchor:
            expect(f"B {tag}: 命中触发条件 (是主城 且 无锚点) -> 新分支会介入",
                   mc and not ac)
        if want_city and want_anchor:
            expect(f"B {tag}: 锚点可见 -> 新分支不介入 (正常流程不受影响)",
                   mc and ac)


def main(argv):
    print("nav_modal_selftest —— 导航模态清障")
    section_a()
    if "--no-real" in argv:
        print("\n=== B 段已跳过 (--no-real) ===")
    else:
        section_b()
    n, ok = len(RESULTS), sum(RESULTS)
    print(f"\n{n-ok} 失败 / {n} 用例" if ok != n else f"\n全部 {n} 项通过")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
