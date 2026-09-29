# -*- coding: utf-8 -*-
"""诊断: 主城"王者之巅"OCR 块中心 vs 各建筑标签位置 (定位误点地下城的根因).

用法:
  python scripts/diag_city_entry.py                      # 用默认主城截图
  python scripts/diag_city_entry.py captures/xxx.png     # 指定截图
  python scripts/diag_city_entry.py --live              # 现拍一张
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot import rank_layout as rk          # noqa: E402
from sj_bot.battler import Battler            # noqa: E402


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    b = Battler(mode="rank", rounds=1)
    if "--live" in sys.argv or not args:
        if "--live" in sys.argv:
            img = b._shot_img()
        else:
            p = ROOT / "captures" / "anomaly_120758_not_rank_hub_rescue1.png"
            img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    else:
        img = cv2.imdecode(np.fromfile(str(ROOT / args[0]), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print("截图读取失败"); return

    items = b._ocr_texts(img, scale=1.0)
    print(f"OCR 块数 = {len(items)}  (图 {img.shape[1]}x{img.shape[0]})")
    print(f"{'text':<26}{'cx':>7}{'cy':>7}{'w':>6}{'h':>6}  score")
    marks = ("王者之巅", "地下城", "王者之争", "深渊迷宫", "藏宝之地", "英雄城堡", "矿洞",
             "铁匠铺", "荣誉殿堂", "商店", "魔龙")
    for it in sorted(items, key=lambda z: z["cy"]):
        tag = ""
        for m in marks:
            if m in it["text"]:
                tag = f"  <== {m}"
                break
        x0, y0 = it["x0"], it["y0"]
        w = it["area"] ** 0.5
        print(f"{it['text'][:24]:<26}{it['cx']:>7.0f}{it['cy']:>7.0f}"
              f"{it['area'] ** 0.5:>6.0f}{0:>6}  {it['score']:.2f}{tag}")

    wz = b._find_hint(img, ("王者之巅",), scale=1.0)
    print("\n_find_hint('王者之巅') =>", None if not wz else
          f"text={wz['text']!r} center=({wz['cx']:.0f},{wz['cy']:.0f}) area={wz['area']:.0f}")
    print("_is_city =", b._is_city(img))
    print("_is_pvp_submenu =", b._is_pvp_submenu(img))
    print("_classify_page =", b._classify_page(img))
    print("\n主城建筑标签参考 (2026-09-11 实测): 地下城(550,320) 王者之巅(856,577)")


if __name__ == "__main__":
    main()
