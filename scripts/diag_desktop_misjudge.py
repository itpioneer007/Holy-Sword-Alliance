# -*- coding: utf-8 -*-
"""诊断: 2026-09-13 18:0x 荣耀任务 nav_failed 三连根因。

现象: 游戏被 MuMu 回收落到模拟器桌面后,
  ① _classify_page(桌面帧) == 'main_city' (宽词表误判) 但 _is_city(桌面帧) == False
     -> _enter_pvp_hub 走 else 分支, 日志打出矛盾的 "(分类=main_city)";
  ② 逃生时 _check_game_health() 没判出 gone/background (未触发拉起游戏), 4 次 back 白按;
  ③ 18:02:02 一次分类返回 'session' (桌面帧误判会话失效?)。

本脚本在 真机现状 + 桌面现场帧 上复现三个判据, 只读不点。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402


def main():
    from sj_bot.battler import Battler

    b = Battler(mode="rank", log=lambda lvl, msg: print(f"  [{lvl}] {msg}"))

    print("=== ① 真机现状 (存活探测) ===")
    pkg = b._foreground_pkg()
    print(f"  _foreground_pkg = {pkg!r}  (game_pkg={b.cfg.game_pkg!r})")
    pid = b._game_pid()
    print(f"  _game_pid = {pid!r}")
    print(f"  _check_game_health = {b._check_game_health()!r}")

    print("\n=== ② 桌面现场帧判据 ===")
    p = os.path.join(_ROOT, "captures", "anomaly_180355_escape_failed_nav.png")
    img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
    print(f"  帧 = {os.path.basename(p)}")
    print(f"  _is_city (王者之巅) = {b._is_city(img)}")
    print(f"  _is_pvp_submenu = {b._is_pvp_submenu(img)}")
    print(f"  _is_session_expired = {b._is_session_expired(img)}")
    print(f"  _is_login_screen = {b._is_login_screen(img)}")

    # main_city 宽词表逐词命中明细 (复刻 _is_in_main_city 的全图分支)
    texts = [it["text"] for it in b._ocr_texts(img, scale=1.0)]
    print(f"  全图 OCR {len(texts)} 块:")
    for t in texts:
        hit = [h for h in b._MAIN_CITY_HINTS if h in t]
        mark = f"  <-- 命中 {hit}" if hit else ""
        print(f"    {t!r}{mark}")
    hits = sum(1 for t in texts if any(h in t for h in b._MAIN_CITY_HINTS))
    print(f"  宽词表命中数 = {hits} (阈值 _MAIN_CITY_MIN_HITS={b._MAIN_CITY_MIN_HITS})")

    # tab 快路径 (rank_layout.MAIN_CITY_TAB_ROI)
    import sj_bot.rank_layout as rk
    tab_texts = [it["text"] for it in
                 b._ocr_texts(img, roi=rk.MAIN_CITY_TAB_ROI, scale=1.0)]
    tab_hits = set()
    for t in tab_texts:
        for h in rk.MAIN_CITY_TAB_HINTS:
            if h in t:
                tab_hits.add(h)
    print(f"  tab快路径: ROI文本={tab_texts} 去重命中={sorted(tab_hits)} "
          f"(阈值 {rk.MAIN_CITY_TAB_MIN_HITS})")

    print(f"\n  _classify_page(桌面帧) = {b._classify_page(img)!r}")

    print("\n=== ③ 'session' 误判来源 (18:02:02) ===")
    print("  SESSION_HINTS =", rk.SESSION_HINTS, " ROI =", rk.SESSION_TEXT_ROI)
    sess = b._find_hint(img, rk.SESSION_HINTS, roi=rk.SESSION_TEXT_ROI, scale=1.0)
    print(f"  桌面帧 session 命中 = {sess}")


if __name__ == "__main__":
    main()
