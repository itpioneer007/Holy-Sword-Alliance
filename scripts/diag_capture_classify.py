# -*- coding: utf-8 -*-
"""批量给 captures 里的可疑留图定性 (单次 OCR + 内联判据, 避免重复全图 OCR).

用法: python scripts/diag_capture_classify.py <文件名> [<文件名> ...]
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
    names = sys.argv[1:]
    if not names:
        names = ["anomaly_130244_main_city_rescue1.png",
                 "anomaly_130327_no_pvp_submenu.png",
                 "anomaly_130642_unknown_screen.png",
                 "anomaly_131731_no_battle_ui.png"]
    b = Battler(mode="rank", rounds=1)
    for n in names:
        p = ROOT / "captures" / n
        if not p.exists():
            print(f"[缺失] {n}")
            continue
        img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
        items = b._ocr_texts(img, scale=1.0)
        texts = [it["text"] for it in items]
        joined = " | ".join(texts)

        def has(*kw):
            return [t for t in texts if any(k in t for k in kw)]

        # 内联判据 (与 battler 同源)
        city = has("王者之巅") or len(has(*b._MAIN_CITY_HINTS)) >= 2
        submenu = bool(has("王者之争"))
        hub_title = [it for it in items if "荣耀排位赛" in it["text"]
                     and rk.HUB_TITLE_ROI[0] <= it["cx"] <= rk.HUB_TITLE_ROI[2]
                     and rk.HUB_TITLE_ROI[1] <= it["cy"] <= rk.HUB_TITLE_ROI[3]]
        hub = bool(hub_title) and bool(has(*rk.HUB_UNIQUE_HINTS))
        chapter = has("星数奖励", "BOSS")
        nums = [t for t in texts if "-" in t and any(c.isdigit() for c in t)]
        print(f"\n=== {n} ===")
        print(f"  city={city} submenu={submenu} hub={hub} "
              f"battle={b._is_in_battle(img)} settle={b._detect_settle_stage(img)}")
        print(f"  OCR({len(texts)}): {joined[:280]}")
        if chapter or len(nums) >= 4:
            print(f"  ⚠ 副本关卡页特征: 星数/BOSS={chapter} 章节编号={nums[:8]}")


if __name__ == "__main__":
    main()
