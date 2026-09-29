# -*- coding: utf-8 -*-
"""真实帧回归: 主城「设置浮层」不得被判成主城 (2026-09-23)。

为什么需要它: `_is_in_main_city` 的守卫是**顺序敏感**的 —— 必须挂在 tab 快路径之后、
**全图回退之前**。`classify_page_selftest.py` 里第 2 段能用桩证明这条顺序, 但它证明不了
**真实画面上**的命中组合 (浮层下到底还能读出哪几个主城建筑词)。本脚本用带人工标签的
真实截图跑完整 `_is_in_main_city` / `_classify_page`, 是唯一能证明"真实判定没变"的手段。

样本固化在 `assets/menu_ref/` (不放 captures/ —— 那里会被例行清理掉, 09-23 就是因为
captures/ 被清空导致 nav_retry_selftest 的 _p3_*.png 样本全丢、测试直接崩)。
期望值来自人工看图 + 真机复现, **不来自代码**。

背景 (真机复现链):
  主城 --back--> 设置浮层(MOD包/账号/图鉴/音效/音乐/退出) --back--> 主城
  浮层半透明, 其下 荣誉殿堂/铁匠铺/王者之巅 仍可被 OCR 读出 (3 个 _MAIN_CITY_HINTS)
  => 修复前 _is_in_main_city 判 True => 'main_city' => 去盲点被遮住的「王者之巅」
  => nav_failed, 且因非 'unknown' 逃生分支不触发 (一个 back 就能出的页面反而卡死)。

用法: python scripts/verify_menu_overlay.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from sj_bot.battler import Battler
from sj_bot.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "assets", "menu_ref")


def imread_u(path: str):
    """⚠️ 本机 ROOT 路径含中文: `cv2.imread` 传**绝对路径**会直接读不到 (返回 None,
    且只在 stderr 吐一行 findDecoder 警告) —— 与 cv2.imwrite 的静默失败同源。
    必须走 np.fromfile + imdecode。本文件用绝对路径拼 REF, 所以这步是必需的。
    (项目既有 verify_classify_real / diag_roi_audit / verify_tpl_gate 都是这个写法。)"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)

# (帧, 人工标签, 期望 _is_in_main_city, 期望 _classify_page)
CASES = [
    ("overlay_a.png",        "设置浮层 (主城按back弹出)", False, "unknown"),
    ("overlay_b.png",        "设置浮层 (独立第二次复现)", False, "unknown"),
    ("main_city_a.png",      "主城 (浮层back退回后)",     True,  "main_city"),
    ("main_city_b.png",      "主城 (另一次退回后)",       True,  "main_city"),
    ("splash_after_login.png", "登录后开场活动页(契约战令)", False, "unknown"),
    ("login.png",            "登录页",                    False, "login"),
]


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    warns = []
    b.log = lambda lv, msg: warns.append(f"[{lv}] {msg}")
    fails = 0
    for fn, label, exp_mc, exp_page in CASES:
        p = os.path.join(REF, fn)
        img = imread_u(p)
        if img is None:
            print(f"SKIP  {label}: 样本缺失 {p}")
            fails += 1
            continue
        t = time.time()
        mc = b._is_in_main_city(img)
        dt_mc = time.time() - t
        t = time.time()
        page = b._classify_page(img)
        dt_pg = time.time() - t
        ok = (mc == exp_mc) and (page == exp_page)
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label:26s} "
              f"main_city={str(mc):5s}(期望{exp_mc!s:5s}) "
              f"classify={page:10s}(期望{exp_page:10s})  "
              f"[{dt_mc:.1f}s/{dt_pg:.1f}s]")
        for w in warns:
            print(f"        日志: {w}")
        warns.clear()

    total = len(CASES)
    print(f"\n{total - fails}/{total} 通过")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
