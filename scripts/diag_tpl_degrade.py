# -*- coding: utf-8 -*-
"""模板评分退化实验: 确定"直接判未命中"的安全下界 (2026-09-13 P2)。

背景: `end_btn.png` / `back_btn.png` 在真实帧上正样本 0.999~1.000、负样本 ≤0.491 /
      ≤0.348 —— 存在干净空隙, 于是可以设计**双阈值门控**:
          score >= hi  -> 命中 (不跑 OCR)
          score <= lo  -> 未命中 (不跑 OCR)
          否则         -> 跑 OCR 兜底
      "score <= lo 就跳过 OCR" 会引入漏判风险: 若某天真结束页因光照/压缩掉到 lo 以下,
      原来靠 OCR 兜底能救回来, 门控后就漏了。

本脚本量化这个风险: 对真实正样本注入**模拟退化** (亮度/对比度/模糊/JPEG 压缩/噪声),
看分数最低能掉到多少。只要最坏退化仍明显高于负样本上界, lo 就有安全余量。

用法: python scripts/diag_tpl_degrade.py
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

CASES = [
    ("rank_4_end.png", "end_btn.png", rk.END_ROI),
    ("anomaly_104531_no_end.png", "end_btn.png", rk.END_ROI),
    ("rank_5_back.png", "back_btn.png", rk.BACK_ROI),
]

# 已知负样本上界 (来自 diag_tpl_scores.py)
NEG_MAX = {"end_btn.png": 0.491, "back_btn.png": 0.348}


def load(p):
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def score(img, tpl_path, roi):
    """与生产 `Battler._tpl_score` 一致的**灰度**匹配 (见 diag_tpl_scores.py 的修正说明:
    彩色三通道匹配会得到不同分布, 诊断必须复刻生产算法)。"""
    tpl = load(tpl_path)
    if tpl is None:
        return None
    tpl = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = roi
    crop = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    th, tw = tpl.shape[:2]
    if th > crop.shape[0] or tw > crop.shape[1]:
        return None
    return float(cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED).max())


def variants(img):
    """产出 (名字, 变换后的图) —— 模拟现场可能出现的光照/编码退化。"""
    out = [("原图", img)]
    for b in (-60, -40, -25, 25, 40, 60):
        out.append((f"亮度{b:+d}", cv2.convertScaleAbs(img, alpha=1.0, beta=b)))
    for a in (0.7, 0.85, 1.15, 1.3):
        out.append((f"对比度x{a}", cv2.convertScaleAbs(img, alpha=a, beta=0)))
    for a, b in ((0.85, 25), (1.15, -25), (0.9, -35)):
        out.append((f"对比x{a}亮{b:+d}", cv2.convertScaleAbs(img, alpha=a, beta=b)))
    for k, s in ((3, 0.8), (5, 1.2), (7, 1.6)):
        out.append((f"高斯模糊{k}", cv2.GaussianBlur(img, (k, k), s)))
    for q in (75, 55, 35):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if ok:
            out.append((f"JPEG q{q}", cv2.imdecode(buf, cv2.IMREAD_COLOR)))
    rng = np.random.default_rng(7)
    for sig in (6, 12, 20):
        noisy = np.clip(img.astype(np.int16)
                        + rng.normal(0, sig, img.shape).astype(np.int16),
                        0, 255).astype(np.uint8)
        out.append((f"高斯噪声σ{sig}", noisy))
    # 组合退化 (最坏情况)
    comb = cv2.convertScaleAbs(img, alpha=0.85, beta=30)
    comb = cv2.GaussianBlur(comb, (5, 5), 1.2)
    ok, buf = cv2.imencode(".jpg", comb, [cv2.IMWRITE_JPEG_QUALITY, 55])
    if ok:
        out.append(("组合(对比0.85亮+30模糊5+JPEG55)",
                    cv2.imdecode(buf, cv2.IMREAD_COLOR)))
    return out


def main():
    print("=" * 92)
    print("模板评分退化实验  (NEG_MAX = 负样本实测上界)")
    print("=" * 92)
    for fn, tpl, roi in CASES:
        img = load(os.path.join(CAP, fn))
        if img is None:
            print(f"[缺图] {fn}")
            continue
        if img.shape[:2] != (900, 1600):
            print(f"[跳过: 尺寸] {fn}")
            continue
        neg = NEG_MAX[tpl]
        scores = []
        for name, v in variants(img):
            s = score(v, os.path.join(ASSETS, tpl), roi)
            if s is not None:
                scores.append((s, name))
        scores.sort()
        worst_s, worst_n = scores[0]
        print(f"\n### {fn}  模板 {tpl}  负样本上界 {neg:.3f}")
        print(f"   最低分 = {worst_s:.3f}  ({worst_n})")
        print(f"   最高分 = {scores[-1][0]:.3f}  ({scores[-1][1]})")
        below = [(round(s, 3), n) for s, n in scores if s <= neg]
        print(f"   低于负样本上界的退化: {below if below else '无'}")
        # 建议 lo: 取 负样本上界 与 最坏正样本分 之间, 且留 0.10 余量
        if worst_s > neg:
            lo_safe = neg + (worst_s - neg) * 0.5
            print(f"   ⇒ 可分: 空隙 ({neg:.3f}, {worst_s:.3f}); "
                  f"建议 lo≈{lo_safe:.2f} (取中点), 门控下仍不会漏判本组退化样本")
        else:
            print(f"   ✗ 最坏退化 {worst_s:.3f} 已 <= 负样本上界 {neg:.3f} "
                  f"⇒ **不能**用门控跳过 OCR, 该模板对光照不稳")
    print("\n" + "=" * 92)


if __name__ == "__main__":
    main()
