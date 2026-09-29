# -*- coding: utf-8 -*-
"""弹窗/浮层「廉价预筛」特征寻优 (2026-09-13 P2)。

问题: `_classify_page` 在主页帧上 ~28s 花在恒为负的 daily_limit/cooldown/reward/
      end/back 五项 **OCR** 上。OCR 是唯一昂贵手段, 所以想找一条**毫秒级**的判据,
      在"肯定不是浮层页"时把它们整体跳过 (继续保留 OCR 作为 True 分支的正常路径)。

候选特征 (全部 cv2/numpy, 每次 ~10ms 量级):
  A. 中心区亮度均值            B. 中心区亮度标准差 (浮层通常低方差)
  C. 高亮像素占比              D. 最大近均匀连通块面积占比
  E. 中心区与边缘区亮度差      F. 中心区饱和像素占比

本脚本只做**特征统计与分离度检查**, 不改任何代码; 有干净阈值才谈落地。

用法: python scripts/diag_overlay_features.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "captures")

# 中心区 = 五项 OCR 判据共同覆盖的区域
CENTER = (400, 240, 1200, 470)
# 边缘区 = 同样大小但贴边 (用作对比基准)
EDGE = (400, 640, 1200, 870)

# (帧, 是否"浮层/结算类"页面 —— 即期望五项 OCR 里至少一项可能命中)
FRAMES = [
    # ---- 期望 True: 弹窗 / 结算链 ----
    ("rank_3_reward.png", True),
    ("rank_4_end.png", True),
    ("rank_5_back.png", True),
    ("anomaly_121044_settle_loop_reward.png", True),
    ("anomaly_141443_settle_loop_reward.png", True),
    ("anomaly_174041_cooldown_nav_fail.png", True),
    ("anomaly_104531_no_end.png", True),
    ("anomaly_111325_no_end.png", True),
    # ---- 期望 False: 主页 / 主城 / 战斗 / 其它 ----
    ("_p1_after.png", False),
    ("rank_0_hub.png", False),
    ("rank_hub_now.png", False),
    ("rank_1_matched.png", False),
    ("_p0_baseline.png", False),
    ("anomaly_152646_main_city_loop.png", False),
    ("anomaly_103234_main_city_rescue1.png", False),
    ("rank_2_battle.png", False),
    ("rank_hub_clean.png", False),
    ("rank_sub_now.png", False),
    ("anomaly_143634_pve_stage_detected.png", False),
]


def load(n):
    p = os.path.join(CAP, n)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def feats(img, roi):
    x0, y0, x1, y1 = roi
    g = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    # 最大近均匀块: 先用中值模糊压噪, 再按与原图差异做阈值, 取最大连通域
    blur = cv2.medianBlur(g, 9)
    diff = cv2.absdiff(g, blur)
    _, mask = cv2.threshold(diff, 8, 255, cv2.THRESH_BINARY_INV)  # 差异小=均匀
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    uniform = 0.0
    if n > 1:
        # 去掉标签 0 (背景), 取最大块
        areas = stats[1:, cv2.CC_STAT_AREA]
        uniform = float(areas.max()) / mask.size
    return {
        "mean": float(g.mean()),
        "std": float(g.std()),
        "bright": float((g > 170).mean()),          # 高亮占比
        "sat": float((hsv[:, :, 1] > 90).mean()),   # 饱和占比
        "uniform": uniform,
    }


def main():
    rows = []
    for name, want in FRAMES:
        img = load(name)
        if img is None:
            print(f"  [缺图] {name}")
            continue
        if img.shape[:2] != (900, 1600):
            print(f"  [跳过: 尺寸] {name}")
            continue
        f = feats(img, CENTER)
        e = feats(img, EDGE)
        f["mean_minus_edge"] = f["mean"] - e["mean"]
        rows.append((name, want, f))

    keys = ["mean", "std", "bright", "sat", "uniform", "mean_minus_edge"]
    print("=" * 118)
    print(f"中心区 {CENTER} 特征  (want=True 表示期望是浮层/结算页)")
    print("=" * 118)
    print(f"{'帧':40s}{'want':6s}" + "".join(f"{k:>14s}" for k in keys))
    for name, want, f in rows:
        print(f"{name:40s}{str(want):6s}"
              + "".join(f"{f[k]:14.4f}" for k in keys))

    print("\n---- 单特征分离度 (取阈值使 True/False 完全分开; 分不开则报重叠) ----")
    for k in keys:
        tv = [f[k] for _, w, f in rows if w]
        fv = [f[k] for _, w, f in rows if not w]
        if not tv or not fv:
            continue
        # 找是否存在阈值完全分开
        sep = None
        if min(tv) > max(fv):
            sep = ("True > 阈值 > False", max(fv), min(tv))
        elif max(tv) < min(fv):
            sep = ("True < 阈值 < False", max(tv), min(fv))
        if sep:
            print(f"  {k:16s} 可分: {sep[0]}  阈值取 ({sep[1]:.4f}, {sep[2]:.4f})")
        else:
            lo_t, hi_t = min(tv), max(tv)
            lo_f, hi_f = min(fv), max(fv)
            print(f"  {k:16s} ✗ 重叠: True 范围 [{lo_t:.4f},{hi_t:.4f}] "
                  f"False 范围 [{lo_f:.4f},{hi_f:.4f}]")


if __name__ == "__main__":
    main()
