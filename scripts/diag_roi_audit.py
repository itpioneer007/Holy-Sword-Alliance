# -*- coding: utf-8 -*-
"""OCR ROI 可用性审计 (2026-09-13)。

发现: rapidocr 的 det 对**薄条带**失效 —— 实测同一帧同一内容,
h=176 返回 0 块, h=192 返回 10 块。故所有高度 <192 的 OCR ROI
都是**静默死代码** (判定恒为 False), 必须逐个在真实正样本上验证。

用法:
    python scripts/diag_roi_audit.py
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

# (ROI 值, ROI 名, 关键词集, [正样本帧...])
CASES = [
    (rk.SESSION_TEXT_ROI, "SESSION_TEXT_ROI", rk.SESSION_HINTS,
     ["anomaly_121832_session_settle_reward.png",
      "anomaly_122415_session_nav_entry.png"]),
    (rk.PVE_TITLE_ROI, "PVE_TITLE_ROI", rk.PVE_STAGE_HINTS,
     ["anomaly_143634_pve_stage_detected.png",
      "anomaly_150852_pve_stage_detected.png"]),
    (rk.PVE_STAR_ROI, "PVE_STAR_ROI", rk.PVE_STAGE_HINTS,
     ["anomaly_143634_pve_stage_detected.png",
      "anomaly_150852_pve_stage_detected.png"]),
    (rk.HUB_BTN_ROI, "HUB_BTN_ROI", ("单人匹配", "取消匹配"),
     ["rank_0_hub.png", "rank_hub_clean2.png"]),
    (rk.MAIN_CITY_TAB_ROI, "MAIN_CITY_TAB_ROI", rk.MAIN_CITY_TAB_HINTS,
     ["anomaly_152646_main_city_loop.png",
      "anomaly_103234_main_city_rescue1.png"]),
    (rk.LOGIN_HINT_ROI, "LOGIN_HINT_ROI", rk.LOGIN_HINTS,
     ["20260907_190242_login_inspect.png"]),
    # 2026-09-23 新增: 主城「设置浮层」ROI。样本用 '@' 前缀指向 assets/menu_ref/ ——
    # captures/ 会被例行清理 (09-23 就是因为 captures/ 被清空, nav_retry_selftest 的
    # _p3_*.png 样本全丢、测试直接崩), 而这条 ROI 的回归必须长期可跑, 故样本固化在 assets/。
    (rk.MENU_ROI, "MENU_ROI", rk.MENU_HINTS,
     ["@assets/menu_ref/overlay_a.png", "@assets/menu_ref/overlay_b.png"]),
    (rk.HUB_TITLE_ROI, "HUB_TITLE_ROI", ("荣耀排位赛",),
     ["rank_0_hub.png"]),
]


def imread_u(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def resolve_frame(fn):
    """帧名默认取 captures/; 以 '@' 开头表示**相对项目根** (用于 assets/ 下固化的样本)。"""
    if fn.startswith("@"):
        return os.path.join(ROOT, fn[1:].replace("/", os.sep))
    return os.path.join(CAP, fn)


def ocr_roi(ocr, img, roi):
    x0, y0, x1, y1 = roi
    crop = img[y0:y1, x0:x1]
    t0 = time.time()
    res, _ = ocr(crop)
    return (res or []), time.time() - t0


def main():
    ocr = make_ocr()
    print("=" * 84)
    print("OCR ROI 可用性审计  (det 薄条带下限: h>=192)")
    print("=" * 84)
    dead = []
    for roi, name, hints, frames in CASES:
        h = roi[3] - roi[1]
        w = roi[2] - roi[0]
        print(f"\n### {name} {roi}  h={h} w={w}  "
              f"{'⚠ 低于阈值' if h < 192 else ''}")
        for fn in frames:
            p = resolve_frame(fn)
            if not os.path.exists(p):
                print(f"   [缺帧] {fn}")
                continue
            img = imread_u(p)
            if img is None:
                print(f"   [解码失败] {fn}")
                continue
            res, dt = ocr_roi(ocr, img, roi)
            texts = [t for _, t, _ in res]
            hit = [h_ for h_ in hints
                   if any(h_ in t for t in texts)]
            allhit = [h_ for h_ in hints
                      if any(h_ in t for t in imread_and_all(ocr, p, img))]
            status = "可检出" if hit else ("⚠ 本 ROI 检不出" +
                                        ("(全图也检不出→帧非正样本)" if not allhit
                                         else "(全图可检出→ROI 失效)"))
            print(f"   {fn:46s} {len(res):3d}块 {dt:5.2f}s  "
                  f"命中={hit}  {status}")
            if not res:
                dead.append((name, fn))
    print("\n" + "=" * 84)
    print("结论:")
    for name, fn in dead:
        print(f"  ☠ {name} 在 {fn} 上返回 0 块 → 该 ROI 的 OCR 判据恒为 False")
    print("(空表示无静默死 ROI)")
    print("=" * 84)


_ALL_CACHE = {}


def imread_and_all(ocr, path, img):
    """全图 OCR (缓存) 用于区分「ROI 失效」与「帧本身不是正样本」。"""
    if path not in _ALL_CACHE:
        res, _ = ocr(img)
        _ALL_CACHE[path] = [t for _, t, _ in (res or [])]
    return _ALL_CACHE[path]


if __name__ == "__main__":
    main()
