# -*- coding: utf-8 -*-
"""验证 _is_on_list 新判据 (短「挑战」块计数) 与加宽后的扫描 ROI。

背景 (2026-09-13 arena 首跑双翻车, 详见 reports/nav_perf_20260913.md §12):
  旧判据 `蓝青矩形 and 子串'挑战'` 在主城误判 True (「每日挑战1次竞技场」+ 详情按钮),
  在真实列表误判 False (按钮低于旧 ROI 下沿 400)。
本脚本对全部标注帧跑**生产函数** `_is_on_list` 与 `scan_opponent_list`,
任何阈值/ROI 改动后必须重跑。
"""
import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from sj_bot.battler import Battler  # noqa: E402

CAP = os.path.join(_ROOT, "captures")

# (文件名, 期望 _is_on_list, 期望扫描人数下限; 人数下限 None 表示不检查)
CASES = [
    # 正样本: 真实对战列表
    ("anomaly_160810_not_list.png", True, 1),   # 09-13 真实列表 (半透明横幅叠加, 按钮 y 248~630)
    ("arena_page_now.png",          True, 5),   # 09-11 列表 (旧布局, 按钮 y 99~438)
    # 负样本: 干扰页
    ("_arena_live1.png",            False, None),  # 主城 (翻车现场: 每日挑战长句 + 详情按钮)
    ("_p3_v1.png",                  False, None),  # 二级菜单
    ("rank_hub_clean2.png",         False, None),  # 排位主页
    ("_p3_s2.png",                  False, None),  # 主城另一帧
]

RESULTS = []


def check(name, got, expect):
    ok = got == expect
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r} (期望 {expect!r})")


def load(fn):
    p = os.path.join(CAP, fn)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def main():
    b = Battler(mode="arena")
    b.stop_evt = threading.Event()
    b.log = lambda lv, m: None

    print("=== _is_on_list (生产函数, 全部标注帧) ===")
    for fn, want_list, _min_n in CASES:
        img = load(fn)
        if img is None:
            check(f"{fn} 在库", False, True)
            continue
        check(f"{fn} -> on_list={want_list}", b._is_on_list(img), want_list)

    print("\n=== scan_opponent_list (正样本帧人数) ===")
    for fn, want_list, min_n in CASES:
        if not want_list or min_n is None:
            continue
        img = load(fn)
        if img is None:
            check(f"{fn} 在库", False, True)
            continue
        opps = scan_count = None
        from sj_bot.vision import scan_opponent_list
        t0 = __import__("time").time()
        opps = scan_opponent_list(img)
        dt = __import__("time").time() - t0
        names = [o.name for o in opps][:4]
        check(f"{fn} 扫描 >= {min_n} 人", (len(opps) >= min_n), True)
        print(f"      实得 {len(opps)} 人 {dt:.1f}s: {names}")

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} 通过")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
