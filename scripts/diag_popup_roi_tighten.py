# -*- coding: utf-8 -*-
"""弹窗判据 ROI 收紧实验 (2026-09-13 P2)。

目的: `_classify_page` 在主页帧上 ~28s 花在**恒为负**的 reward/end/back OCR 上。
      OCR 成本 ≈ det 面积 + **ROI 内文本块数**, 而现行 ROI 都开得很宽
      (reward 1000x220 / end 800x330 / back 800x170), 在主页帧上把一堆无关文本
      一起喂给了 rec。按实测文字坐标收紧后应当显著变便宜, 且**判定不变**。

安全性要求 (二选一不可):
  - 在**正样本**上仍然命中 (不能少召回)
  - 在**负样本**上仍然不命中 (不能多召回)

用法: python scripts/diag_popup_roi_tighten.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from sj_bot import rank_layout as rk
from sj_bot.battler import make_ocr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "captures")

POS = {
    "reward": (rk.REWARD_HINTS, ["rank_3_reward.png",
                                 "anomaly_121044_settle_loop_reward.png",
                                 "anomaly_141443_settle_loop_reward.png"]),
    "end": (rk.END_HINTS, ["rank_4_end.png"]),
    "back": (rk.BACK_HINTS, ["rank_5_back.png"]),
}
NEG = ["_p1_after.png", "rank_0_hub.png", "rank_hub_now.png", "rank_1_matched.png",
       "_p0_baseline.png", "anomaly_152646_main_city_loop.png",
       "rank_2_battle.png", "rank_hub_clean.png"]

# 候选 ROI: 按全图 OCR 实测文字 bbox 居中收紧, 并留出漂移余量
CAND = {
    "reward": [rk.REWARD_ROI, (580, 430, 1020, 640), (600, 450, 1000, 620)],
    "end":    [rk.END_ROI, (600, 600, 1050, 810), (620, 610, 1040, 800)],
    "back":   [rk.BACK_ROI, (780, 690, 1120, 895), (800, 700, 1100, 890)],
}


def load(n):
    p = os.path.join(CAP, n)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def main():
    ocr = make_ocr()
    print("=" * 92)
    print("弹窗判据 ROI 收紧实验")
    print("=" * 92)
    for kind, rois in CAND.items():
        hints, posf = POS[kind]
        print(f"\n######## {kind}  关键词={hints}")
        for roi in rois:
            tag = "现行" if roi == rois[0] else "候选"
            x0, y0, x1, y1 = roi
            h, w = y1 - y0, x1 - x0
            # --- 正样本 ---
            pos_line = []
            for fn in posf:
                img = load(fn)
                if img is None:
                    pos_line.append(f"{fn}=缺图")
                    continue
                t0 = time.time()
                res, _ = ocr(img[y0:y1, x0:x1])
                dt = time.time() - t0
                got = [t for _, t, _ in (res or [])
                       if any(hh in t for hh in hints)]
                pos_line.append(f"{fn[:26]} {len(res or []):2d}块 {dt:5.2f}s "
                                f"{'命中' if got else '☠未命中'}")
            # --- 负样本 ---
            neg_hit = []
            neg_cost = []
            for fn in NEG:
                img = load(fn)
                if img is None:
                    continue
                t0 = time.time()
                res, _ = ocr(img[y0:y1, x0:x1])
                neg_cost.append(time.time() - t0)
                got = [t for _, t, _ in (res or [])
                       if any(hh in t for hh in hints)]
                if got:
                    neg_hit.append(f"{fn}->{got}")
            avg = sum(neg_cost) / len(neg_cost) if neg_cost else 0
            print(f"  [{tag}] {roi} h={h:3d} w={w:4d}")
            for ln in pos_line:
                print(f"      正: {ln}")
            print(f"      负: {len(neg_cost)} 帧 平均 {avg:5.2f}s (最大 {max(neg_cost or [0]):5.2f}s)"
                  f"  误命中={neg_hit if neg_hit else '无'}")
    print("\n" + "=" * 92)
    print("判读: 候选要同时满足「正样本命中」+「负样本 0 误命中」+「平均耗时明显更低」才可采用。")


if __name__ == "__main__":
    main()
