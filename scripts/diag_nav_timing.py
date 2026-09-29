# -*- coding: utf-8 -*-
"""导航耗时诊断: 对比条带化前后的 _classify_page 冷启动耗时。

用法:
    python scripts/diag_nav_timing.py

设计说明 (2026-09-13):
- OCR 冷启动成本取决于**输入张量形状的首次分配**, 同形状重复调用会被 ONNX 复用。
  所以"连续测多次再求和"会严重低估 —— 必须每个条件在**独立进程**里测一次,
  或至少先跳过预热轮次。本脚本对每个条件只信任第一次计时。
- 对比口径:
    old_hub   = _find_hint(img, HUB_UNIQUE_HINTS, scale=1.0)   全图 (P1 前的写法)
    new_hub   = _find_hint(img, HUB_UNIQUE_HINTS, roi=HUB_STRIP_ROI, scale=1.0)
    old_city  = _ocr_texts(img, scale=1.0) 全图
    new_city  = _ocr_texts(img, roi=MAIN_CITY_TAB_ROI, scale=1.0)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from sj_bot import rank_layout as rk
from sj_bot.battler import Battler, make_ocr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "captures")


def imread_u(path):
    """cv2.imread 读不了中文路径。"""
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


FRAMES = [
    ("rank_0_hub.png", True, "排位主页(正)"),
    ("rank_hub_clean2.png", True, "排位主页(正)"),
    ("rank_hub_clean.png", False, "排行榜(负)"),
    ("anomaly_152646_main_city_loop.png", False, "主城(负)"),
]


def main():
    b = Battler.__new__(Battler)
    # 只装配 OCR 所需的最小现场, 避免起 adb (ocr 是懒加载 property, 故填 _ocr)
    b._ocr = make_ocr()
    b._ocr_cache = {}
    b._ocr_cache_frame = None

    print("=" * 78)
    print("导航耗时诊断 — 条带化前后 (每项计时前清空同帧缓存, 保证真实未命中)")
    print("=" * 78)

    tot_old = 0.0
    tot_new = 0.0
    bad = 0
    for fn, expect_hub, label in FRAMES:
        p = os.path.join(CAP, fn)
        if not os.path.exists(p):
            print(f"[跳过] {fn} 不存在")
            continue
        img = imread_u(p)
        if img is None:
            print(f"[跳过] {fn} 解码失败")
            continue
        h, w = img.shape[:2]
        if (w, h) != (1600, 900):
            print(f"[跳过] {fn} 尺寸 {w}x{h} != 1600x900")
            continue

        def timed(fn_call):
            """清空同帧缓存后计时 (帧身份保留, 只清键)。"""
            b._ocr_cache_frame = img
            b._ocr_cache = {}
            t0 = time.time()
            r = fn_call()
            return r, time.time() - t0

        old_hub, t_old_hub = timed(
            lambda: b._find_hint(img, rk.HUB_UNIQUE_HINTS, scale=1.0) is not None)
        new_hub, t_new_hub = timed(
            lambda: b._find_hint(img, rk.HUB_UNIQUE_HINTS,
                                 roi=rk.HUB_STRIP_ROI, scale=1.0) is not None)
        n_old, t_old_city = timed(lambda: len(b._ocr_texts(img, scale=1.0)))
        n_new, t_new_city = timed(
            lambda: len(b._ocr_texts(img, roi=rk.MAIN_CITY_TAB_ROI, scale=1.0)))

        agree = "OK" if new_hub == old_hub else "!! 条带与全图不一致"
        verdict = "OK" if new_hub == expect_hub else "!! 判定与期望不符"
        if agree != "OK" or verdict != "OK":
            bad += 1
        tot_old += t_old_hub + t_old_city
        tot_new += t_new_hub + t_new_city

        print(f"\n[{fn}] {label}  期望hub={expect_hub}")
        print(f"  hub  全图 {t_old_hub:6.2f}s -> {old_hub}  |  "
              f"条带 {t_new_hub:6.2f}s -> {new_hub}   {agree} / {verdict}")
        print(f"  city 全图 {t_old_city:6.2f}s ({n_old}块) | "
              f"tab 条带 {t_new_city:6.2f}s ({n_new}块)")
        print(f"  本帧两条判据合计: 旧 {t_old_hub + t_old_city:6.2f}s -> "
              f"新 {t_new_hub + t_new_city:6.2f}s")

    print("\n" + "=" * 78)
    print(f"合计 (hub判据 + city判据): 旧 {tot_old:.2f}s -> 新 {tot_new:.2f}s"
          f"   (提速 {tot_old / tot_new if tot_new else 0:.2f}x)")
    print(f"不一致/期望不符帧数: {bad}")
    print("=" * 78)


if __name__ == "__main__":
    main()
