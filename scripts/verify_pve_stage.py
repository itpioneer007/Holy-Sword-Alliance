# -*- coding: utf-8 -*-
"""验证 PvE 副本关卡页判据 (_is_pve_stage) 与 _classify_page 的分离度.

正样本 = 地下城副本章节图 (引擎绝不该在的页面)
负样本 = 主城 / 排位主页 / 战斗页 / 宝箱结算页 —— 都不得被判成 pve_stage

用法: python scripts/verify_pve_stage.py
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.battler import Battler            # noqa: E402

POS = ["anomaly_131731_no_battle_ui.png",
       "anomaly_121526_no_battle_ui.png",
       "anomaly_134647_no_battle_ui.png"]
NEG = ["anomaly_120758_not_rank_hub_rescue1.png",   # 主城
       "anomaly_130244_main_city_rescue1.png",      # 主城
       "rank_hub_clean.png",                        # 排位主页
       "rank_0_hub.png",                            # 排位主页
       "anomaly_111324_not_rank_hub.png",           # 战斗页
       "rank_3_reward.png"]                         # 宝箱结算页


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    ok_all = True
    print(f"{'页面':<44}{'pve':>6}{'分类':>12}  期望")
    for tag, names, expect in (("POS", POS, True), ("NEG", NEG, False)):
        for n in names:
            p = ROOT / "captures" / n
            if not p.exists():
                print(f"  [skip] {n} 不存在"); continue
            img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
            pve = b._is_pve_stage(img)
            cp = b._classify_page(img)
            if expect:
                ok = pve and cp == "pve_stage"
            else:
                ok = (not pve) and cp != "pve_stage"
            ok_all &= ok
            print(f"{tag} {n[:40]:<40}{str(pve):>6}{cp:>12}  "
                  f"{'pve_stage' if expect else '非pve_stage':<12}"
                  f"{'OK' if ok else '★FAIL★'}")
    print("\n" + ("全部通过" if ok_all else "存在不符样本, 判据需调整"))


if __name__ == "__main__":
    main()
