# -*- coding: utf-8 -*-
"""真机端到端验证: 主城「设置浮层」的一键逃生 (2026-09-23 健壮性加固后复跑).

验证目标 (用户口径: "在这个界面只需要点击一下back键就能回到你能识别的主页面"):
  1. 在主城按一次 back -> 复现「设置浮层」(MOD包/账号/图鉴/音效/音乐/退出)
  2. 修复前: `_classify_page` 误判为 'main_city' -> 引擎会去盲点被遮住的「王者之巅」
  3. 修复后: 应判为 'unknown' -> 落进逃生分支 -> back 一次 -> 回到 'main_city'

用法: python scripts/live_menu_overlay_escape_test.py
注意: 本脚本会在**真机上按键** (back) 并可能进入/退出浮层, 不消耗任何战斗资源。
"""
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.battler import Battler            # noqa: E402

OUT = ROOT / "outputs"


def save(img, name: str) -> None:
    try:
        cv2.imencode(".png", img)[1].tofile(str(OUT / f"{name}.png"))
    except Exception as e:
        print(f"  ⚠ 存图异常 {name}: {e}")


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    b.log = lambda lv, msg: print(f"      >> [{lv}] {msg}")

    img = b._shot_img()
    if img is None:
        print("截图失败")
        return
    save(img, "menu_t0_start")
    t = time.time()
    start = b._classify_page(img)
    print(f"[0] 起始页 (主城?)  classify={start}  ({time.time()-t:.1f}s)")
    if start != "main_city":
        print("    ⚠ 起始不在主城, 请先手动回到主城再跑 (否则本测试前置不成立)")
        return

    print("\n[1] 在主城按一次 back -> 应当弹出设置浮层")
    b._press_back()
    time.sleep(2.5)
    img = b._shot_img()
    save(img, "menu_t1_overlay")
    t = time.time()
    page_overlay = b._classify_page(img)
    t1 = time.time() - t
    print(f"    浮层页分类 = {page_overlay}   ({t1:.1f}s)")
    print(f"    -> 期望 'unknown' (修复前是 'main_city') : "
          f"{'✔ 通过' if page_overlay == 'unknown' else '✘ 未通过'}")

    print("\n[2] 调用引擎通用逃生 (模拟真实调用点: 超时自愈/导航未登记页)")
    t = time.time()
    landed = b._escape_unknown_page("live_menu")
    print(f"    _escape_unknown_page -> {landed}   ({time.time()-t:.1f}s)")
    ok = landed in ("main_city", "hub")
    print(f"    -> 期望回到 main_city/hub : {'✔ 通过' if ok else '✘ 未通过'}")

    img = b._shot_img()
    save(img, "menu_t2_after_escape")
    t = time.time()
    final = b._classify_page(img)
    print(f"\n[3] 逃生后页面 = {final}   ({time.time()-t:.1f}s)")
    print(f"    -> 期望 'main_city' : {'✔ 通过' if final == 'main_city' else '✘ 未通过'}")

    print("\n逐帧图: outputs/menu_t*.png")


if __name__ == "__main__":
    main()
