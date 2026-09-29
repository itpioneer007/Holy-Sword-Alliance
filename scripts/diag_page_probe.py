# -*- coding: utf-8 -*-
"""复现诊断: 对一张截图逐项跑 Battler 的页面判定器, 复刻 _enter_pvp_hub 的三分支.

目的: 证明"战斗页"在 _enter_pvp_hub 里会落入 else 分支 (unknown_screen).
用法: python scripts/diag_page_probe.py [图片路径 ...]  (默认跑指定异常图)
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot import rank_layout as rk            # noqa: E402
from sj_bot.battler import Battler              # noqa: E402


def load(p: str) -> np.ndarray:
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def probe(b: Battler, img: np.ndarray, name: str) -> None:
    print("=" * 72)
    print(f"[{name}]  shape={img.shape}")
    t0 = time.time()
    skip = b._find_skip_btn(img)
    print(f"  _find_skip_btn        = {skip}")
    t1 = time.time()
    hub_fast = b._is_rank_hub(img, fast=True)
    t2 = time.time()
    hub = b._is_rank_hub(img)
    t3 = time.time()
    submenu = b._is_pvp_submenu(img)
    t4 = time.time()
    city = b._is_city(img)
    t5 = time.time()
    on_list = b._is_on_list(img) if b.mode == "arena" else None
    t6 = time.time()
    print(f"  _is_rank_hub(fast)    = {hub_fast}   ({t2-t1:.2f}s)")
    print(f"  _is_rank_hub          = {hub}   ({t3-t2:.2f}s)")
    print(f"  _is_pvp_submenu       = {submenu}   ({t4-t3:.2f}s)")
    print(f"  _is_city              = {city}   ({t5-t4:.2f}s)")
    if on_list is not None:
        print(f"  _is_on_list           = {on_list}   ({t6-t5:.2f}s)")

    # 主城词命中详情 (这是 main_city_rescue 的判据)
    hits = []
    for it in b._ocr_texts(img, scale=1.0):
        t = it["text"]
        for h in b._MAIN_CITY_HINTS:
            if h in t:
                hits.append((h, t, round(it["score"], 2)))
                break
    t7 = time.time()
    print(f"  _is_in_main_city      = {len(hits) >= b._MAIN_CITY_MIN_HITS}"
          f"   (命中 {len(hits)} 个, 阈值 {b._MAIN_CITY_MIN_HITS}, 全图 OCR {t7-t6:.2f}s)")
    for h, t, sc in hits[:12]:
        print(f"      hit: 词'{h}' <- OCR'{t}' ({sc})")

    t8 = time.time()
    page = b._classify_page(img)
    t9 = time.time()
    print(f"  _classify_page        = {page}   ({t9-t8:.2f}s)")
    print(f"  >> _enter_pvp_hub 分支: ", end="")
    if hub:
        print("1) 已在主页 -> True")
    elif submenu:
        print("2) 二级菜单 -> 点模式按钮")
    elif city:
        print("3) 主城 -> 点王者之巅")
    else:
        print("4) else -> '导航失败: 当前页面非主页/二级菜单/主城' + unknown_screen + False  <<<<")
    print(f"  [总耗时 {time.time()-t0:.2f}s]")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        args = [str(ROOT / "captures" / "anomaly_075847_unknown_screen.png"),
                str(ROOT / "captures" / "anomaly_075732_main_city_rescue1.png")]
    b = Battler(mode="rank", rounds=1)
    for a in args:
        p = Path(a)
        if not p.is_absolute():
            p = ROOT / a
        if not p.exists():
            print(f"[skip] not found: {p}")
            continue
        probe(b, load(str(p)), p.name)


if __name__ == "__main__":
    main()
