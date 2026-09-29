# -*- coding: utf-8 -*-
"""end/back 模板评分分布 (2026-09-13 P2)。

动机: `_classify_page` 里 end/back 的写法是 `模板 OR OCR 回退`。模板只要 0.07s,
      OCR 回退要 3~8s。回退之所以存在, 是怕模板在光照/分辨率变化时漏判。
      若实测**正样本分数与负样本分数之间存在干净的空隙**, 就可以用**双阈值门控**:
        score >= hi   -> 直接判定命中 (不跑 OCR)
        score <= lo   -> 直接判定未命中 (不跑 OCR)
        lo < s < hi   -> 才跑 OCR 兜底 (保守, 不丢召回)
      空隙越干净, 落在中间的帧越少, OCR 回退就越少被触发 —— 且**召回不降**。

注意: 这不是"用模板替代 OCR", 而是"把 OCR 只在模糊区间才跑"。

用法: python scripts/diag_tpl_scores.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from sj_bot import rank_layout as rk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "captures")
ASSETS = os.path.join(ROOT, "assets")

# (帧, 期望)
CASES = [
    # end
    ("rank_4_end.png", "end"),
    # ⚠️ 文件名骗人: 该帧**确实是 end 页** (画面中央蓝色「比赛结束」按钮 + 背景
    #    "挑战失败" + 宝箱结算层), 2026-09-13 人工看图核实。旧脚本标 "?" 导致它被
    #    当成负样本, 于是 end_btn 报出"负 max 1.000 / 无干净空隙"的假结论 ——
    #    阈值若据此设定会错误放宽。标签已改正。
    ("anomaly_104531_no_end.png", "end"),
    # 该帧是**荣耀排行榜**页 (「荣耀排行」1/893), 真负样本 (end 0.247 / back 0.256)。
    ("anomaly_111325_no_end.png", "-"),
    # back
    ("rank_5_back.png", "back"),
    # 负样本
    ("_p1_after.png", "-"),
    ("rank_0_hub.png", "-"),
    ("rank_hub_now.png", "-"),
    ("rank_1_matched.png", "-"),
    ("_p0_baseline.png", "-"),
    ("anomaly_152646_main_city_loop.png", "-"),
    ("rank_2_battle.png", "-"),
    ("rank_hub_clean.png", "-"),
    ("rank_sub_now.png", "-"),
    ("anomaly_143634_pve_stage_detected.png", "-"),
    ("rank_3_reward.png", "-"),
]


def load(p):
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def tpl_score(img, tpl_path, roi):
    """返回 ROI 内 TM_CCOEFF_NORMED 最高分; 模板比 ROI 大则返回 None。

    ⚠️ 2026-09-13 修正: **必须灰度匹配**。生产代码 `Battler._tpl_score` 是
    `BGR2GRAY(帧)` vs `BGR2GRAY(模板)` 单通道匹配; 本脚本原先直接用彩色三通道匹配,
    TM_CCOEFF_NORMED 会在 3 个通道上求和 ⇒ 分布不同 (实测 rank_hub_clean.png:
    彩色 0.491 / 灰度 0.461)。诊断脚本若不复刻生产算法, 阈值就会从错误的分布上取值。
    """
    tpl = load(tpl_path)
    if tpl is None:
        return None, tpl
    tpl = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = roi
    crop = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    th, tw = tpl.shape[:2]
    ch, cw = crop.shape[:2]
    if th > ch or tw > cw:
        return None, tpl
    r = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
    return float(r.max()), tpl


def main():
    print("=" * 96)
    print(f"资产尺寸: " + ", ".join(
        f"{n}={load(os.path.join(ASSETS, n)).shape[:2]}"
        for n in ("end_btn.png", "back_btn.png", "skip_btn.png", "x2_btn.png")
        if load(os.path.join(ASSETS, n)) is not None))
    print(f"END_ROI={rk.END_ROI} h={rk.END_ROI[3]-rk.END_ROI[1]}  "
          f"BACK_ROI={rk.BACK_ROI} h={rk.BACK_ROI[3]-rk.BACK_ROI[1]}")
    print("=" * 96)
    print(f"{'帧':40s}{'期望':8s}{'end_btn':>12s}{'back_btn':>12s}")
    rows = []
    for name, want in CASES:
        img = load(os.path.join(CAP, name))
        if img is None:
            print(f"{name:40s}{want:8s}   [缺图/尺寸不符]")
            continue
        if img.shape[:2] != (900, 1600):
            print(f"{name:40s}{want:8s}   [尺寸 {img.shape[:2]}]")
            continue
        es, _ = tpl_score(img, os.path.join(ASSETS, "end_btn.png"), rk.END_ROI)
        bs, _ = tpl_score(img, os.path.join(ASSETS, "back_btn.png"), rk.BACK_ROI)
        rows.append((name, want, es, bs))
        print(f"{name:40s}{want:8s}"
              + (f"{es:12.3f}" if es is not None else f"{'n/a':>12s}")
              + (f"{bs:12.3f}" if bs is not None else f"{'n/a':>12s}"))

    for label, idx in (("end_btn.png", 2), ("back_btn.png", 3)):
        pos = [r[idx] for r in rows if r[1] == label.split("_")[0] and r[idx] is not None]
        neg = [r[idx] for r in rows if r[1] in ("-", "?") and r[idx] is not None]
        print(f"\n---- {label} ----")
        if pos:
            print(f"  正样本 {len(pos)}: {[round(v,3) for v in sorted(pos)]}  "
                  f"min={min(pos):.3f}")
        if neg:
            print(f"  负样本 {len(neg)}: {[round(v,3) for v in sorted(neg, reverse=True)]}  "
                  f"max={max(neg):.3f}")
        if pos and neg:
            if min(pos) > max(neg):
                lo, hi = max(neg), min(pos)
                print(f"  ⇒ 存在干净空隙 ({lo:.3f}, {hi:.3f}); "
                      f"hi={hi:.3f} lo={lo:.3f} 时中间区间为空 ⇒ OCR 回退 0 次")
            else:
                print(f"  ✗ 无干净空隙 (正 min {min(pos):.3f} <= 负 max {max(neg):.3f}) "
                      f"⇒ 双阈值门控收益有限, 需看中间区间占比")


if __name__ == "__main__":
    main()
