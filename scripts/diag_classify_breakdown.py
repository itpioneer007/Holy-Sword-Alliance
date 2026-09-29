# -*- coding: utf-8 -*-
"""_classify_page 逐条件耗时分解 (2026-09-13)。

用途: 导航 88s 的构成 = 起步前 28s + 点击 10s + 落点断言 50s, 两项都由
`_classify_page` 的"固定顺序逐条检查"决定。本脚本在**真实帧**上逐条计时,
给出"哪些判据在哪些页面上白花了时间", 作为下一步优化的依据。

用法:
    python scripts/diag_classify_breakdown.py [帧...]
默认帧: captures/_p0_baseline.png (主城) / _p1_after.png (返回大厅后的主页)
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
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def main():
    names = sys.argv[1:] or ["_p0_baseline.png", "_p1_after.png"]
    b = Battler.__new__(Battler)
    b._ocr = make_ocr()
    b._ocr_cache = {}
    b._ocr_cache_frame = None
    # _is_login_screen / _is_session_expired 只依赖 OCR; _find_btn_tpl 需要模板目录
    from sj_bot.config import Config
    b.cfg = Config()

    def timed(label, fn, reset=True):
        if reset:
            b._ocr_cache = {}          # 保证每条判据真实未命中缓存
        t0 = time.time()
        try:
            r = fn()
        except Exception as e:
            r = f"<异常 {type(e).__name__}: {e}>"
        d = time.time() - t0
        print(f"    {label:34s} {d:6.2f}s  -> {r}")
        return d

    for nm in names:
        p = os.path.join(CAP, nm)
        if not os.path.exists(p):
            print(f"[缺帧] {nm}")
            continue
        img = imread_u(p)
        if img is None:
            print(f"[解码失败] {nm}")
            continue
        b._ocr_cache_frame = img
        print(f"\n===== {nm}  {img.shape[1]}x{img.shape[0]}")
        tot = 0.0
        tot += timed("1 _is_session_expired", lambda: b._is_session_expired(img))
        tot += timed("2 daily_limit ROI", lambda: b._find_hint(
            img, rk.DAILY_LIMIT_HINTS, roi=rk.DAILY_LIMIT_ROI, scale=1.0) is not None)
        tot += timed("3 cooldown ROI", lambda: b._find_hint(
            img, rk.COOLDOWN_HINTS, roi=rk.COOLDOWN_ROI, scale=1.0) is not None)
        tot += timed("4 reward ROI (仅OCR)", lambda: b._find_hint(
            img, rk.REWARD_HINTS, roi=rk.REWARD_ROI, scale=1.0) is not None)
        tot += timed("5 end (模板)", lambda: b._find_btn_tpl(img, "end_btn.png", rk.END_ROI), False)
        tot += timed("6 end (OCR 回退)", lambda: b._find_hint(
            img, rk.END_HINTS, roi=rk.END_ROI, scale=1.0) is not None)
        tot += timed("7 back (模板)", lambda: b._find_btn_tpl(img, "back_btn.png", rk.BACK_ROI), False)
        tot += timed("8 back (OCR 回退)", lambda: b._find_hint(
            img, rk.BACK_HINTS, roi=rk.BACK_ROI, scale=1.0) is not None)
        tot += timed("9 _is_in_battle", lambda: b._is_in_battle(img))
        tot += timed("10 _is_rank_hub(fast=False)", lambda: b._is_rank_hub(img))
        tot += timed("11 _is_in_main_city", lambda: b._is_in_main_city(img))
        tot += timed("12 _is_login_screen", lambda: b._is_login_screen(img))
        tot += timed("13 _is_pve_stage", lambda: b._is_pve_stage(img))
        print(f"    {'逐条合计':34s} {tot:6.2f}s")
        b._ocr_cache = {}
        t0 = time.time()
        page = b._classify_page(img)
        print(f"    {'_classify_page 实际(带缓存短路)':34s} {time.time()-t0:6.2f}s  -> {page}")


if __name__ == "__main__":
    main()
