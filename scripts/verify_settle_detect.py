# -*- coding: utf-8 -*-
"""验证 _detect_settle_stage 的特异性: 非结算页绝不能误判, 否则会乱点宝箱坐标.

用法: python scripts/verify_settle_detect.py
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot import rank_layout as rk          # noqa: E402
from sj_bot.battler import Battler            # noqa: E402

# (文件, 期望)  None = 非结算页
CASES = [
    ("rank_3_reward.png", "reward"),         # 真宝箱页 (阳性对照)
    ("rank_hub_clean.png", None),            # 擂台页
    ("rank_0_hub.png", None),                # 组队页
    ("calib_11_rank_hub.png", None),         # 排位主页
    ("anomaly_111142_main_city_rescue2.png", None),   # 主城
    ("anomaly_111324_not_rank_hub.png", None),        # 战斗页
    ("live_battle_check.png", None),         # 战斗页
    ("anomaly_112956_unknown_page_end.png", None),    # 荣耀排行页
    ("live_page_check.png", None),           # 主城
]


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    ok_all = True
    for name, expect in CASES:
        p = ROOT / "captures" / name
        if not p.exists():
            print(f"  [skip] {name}")
            continue
        img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
        got = b._detect_settle_stage(img)
        hits = [it["text"] for it in b._ocr_texts(img, roi=rk.REWARD_ROI, scale=1.0)]
        ok = got == expect
        ok_all &= ok
        print(f"{'OK  ' if ok else 'FAIL'} {name[:40]:<40} -> {str(got):<8} "
              f"REWARD_ROI OCR={hits}")
    print("\n" + ("全部正确" if ok_all else "★存在误判, 需收紧 REWARD_HINTS/ROI★"))


if __name__ == "__main__":
    main()
