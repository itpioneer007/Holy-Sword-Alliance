# -*- coding: utf-8 -*-
"""`_is_rank_hub(fast=True)` 与 `fast=False`(条带) 的等价性验证 (2026-09-13)。

动机: `_classify_page` 里 hub 判据用 fast=False 的 HUB_STRIP_ROI, 实测在主页帧上
耗时 15.5s, 是"被踢回主城后重导航 ~88s"中落点断言(50s)的最大单项。
fast=True 只扫 HUB_BTN_ROI(按钮行, 约 1.5~2s) + 标题 ROI, 快 ~8~10x。

fast 的已知风险: 标题 ROI 若误命中二级菜单的"荣耀排位赛"按钮, 而按钮行恰好
不可见, 会误判。本脚本就是要在**全部真实帧**上证明两者结论逐帧一致 —— 不一致
的帧会打印出来, 只有 0 不一致才允许把 _classify_page 切到 fast。

用法: python scripts/verify_hub_fast.py
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

# 与 verify_hub_strip.py 同源的全量帧清单 (期望值: 是否主页)
FRAMES = [
    ("rank_0_hub.png", True), ("rank_hub_clean.png", False),
    ("rank_hub_clean2.png", True), ("rank_hub_now.png", True),
    ("rank_1_matched.png", True),
    ("rank_sub_1.png", False), ("rank_sub_now.png", False),
    ("rank_3_reward.png", False), ("rank_4_end.png", False),
    ("rank_5_back.png", False), ("rank_2_battle.png", False),
    ("arena_page_now.png", False), ("calib_13_arena.png", False),
    ("_diag_after_login2.png", False), ("_diag_before_job.png", False),
    ("_diag_settle_loop.png", False), ("_diag_now.png", False),
    # 2026-09-13 真机新抓帧
    ("_p0_baseline.png", False), ("_p1_after.png", True),
    ("anomaly_121832_session_settle_reward.png", False),
    ("anomaly_143634_pve_stage_detected.png", False),
    ("anomaly_152646_main_city_loop.png", False),
]


def load(n):
    p = os.path.join(CAP, n)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def main() -> int:
    b = object.__new__(Battler)
    b._ocr = make_ocr()
    b._ocr_cache = {}
    b._ocr_cache_frame = None
    bad = 0
    n_ok = 0
    for name, exp in FRAMES:
        img = load(name)
        if img is None:
            print(f"  [缺图] {name}")
            continue
        if img.shape[:2] != (900, 1600):
            print(f"  [跳过: 尺寸] {name}")
            continue
        b._ocr_cache = {}
        b._ocr_cache_frame = None
        t = time.time()
        v_strip = b._is_rank_hub(img, fast=False)
        d_strip = time.time() - t
        b._ocr_cache = {}
        b._ocr_cache_frame = None
        t = time.time()
        v_fast = b._is_rank_hub(img, fast=True)
        d_fast = time.time() - t
        agree = v_strip == v_fast
        good = agree and v_strip == exp
        if not good:
            bad += 1
        else:
            n_ok += 1
        print(f"{'PASS' if good else 'FAIL'}  {name:42s} 期望={str(exp):5s} "
              f"条带={str(v_strip):5s}({d_strip:5.1f}s) fast={str(v_fast):5s}({d_fast:5.1f}s)"
              f"{'' if agree else '   ← 不一致!'}")
    print(f"\n{n_ok}/{n_ok + bad} 通过" + (f" (不一致/不符 {bad})" if bad else " —— fresh/fast 逐帧等价"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
