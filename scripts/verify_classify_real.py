# -*- coding: utf-8 -*-
"""_classify_page 真实帧回归 (2026-09-13)。

为什么需要它: `_classify_page` 的判定**顺序**本身就是逻辑的一部分 ——
2026-09-13 P1b 把廉价的"主城 tab 条带"提到开头短路 (省掉主城帧上 ~33s 的逐条 OCR),
顺序一改, 所有"同时命中多条判据"的页面都可能落到不同分支。mock 自测
(classify_page_selftest.py) 只能验证"组合 -> 结果"的映射, **无法证明真实画面上
的命中组合没变**。本脚本用带标签的真实截图跑完整 `_classify_page`, 是唯一能
证明"顺序调整没有改变真实判定"的手段。

用法:
    python scripts/verify_classify_real.py            # 全量
    python scripts/verify_classify_real.py rank_3_reward.png ...
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from sj_bot.battler import Battler, make_ocr
from sj_bot.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "captures")

# (帧, 期望页面) —— 期望值来自人工看图 + 历史记忆, 不来自代码
FRAMES = [
    # 主页 / 匹配中
    ("rank_0_hub.png", "hub"),
    ("rank_hub_clean2.png", "hub"),
    ("rank_hub_now.png", "hub"),
    ("rank_1_matched.png", "hub"),
    ("_p1_after.png", "hub"),
    # 结算链
    ("rank_3_reward.png", "reward"),
    ("rank_4_end.png", "end"),
    ("rank_5_back.png", "back"),
    # 战斗
    ("rank_2_battle.png", "battle"),
    # 主城
    ("_p0_baseline.png", "main_city"),
    ("anomaly_152646_main_city_loop.png", "main_city"),
    ("anomaly_103234_main_city_rescue1.png", "main_city"),
    ("_diag_after_login2.png", "main_city"),
    # 二级菜单 / 擂台列表 / 排行榜 —— 期望 unknown (未登记, 靠逃生处理)
    ("rank_sub_now.png", "unknown"),
    ("rank_hub_clean.png", "unknown"),
    # 会话弹窗
    ("anomaly_121832_session_settle_reward.png", "session"),
    # 副本关卡
    ("anomaly_143634_pve_stage_detected.png", "pve_stage"),
]


def load(n):
    p = os.path.join(CAP, n)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def main() -> int:
    only = set(sys.argv[1:])
    b = Battler.__new__(Battler)
    b._ocr = make_ocr()
    b._ocr_cache = {}
    b._ocr_cache_frame = None
    b.cfg = Config()
    ok = bad = skip = 0
    detail = []
    for name, exp in FRAMES:
        if only and name not in only:
            continue
        img = load(name)
        if img is None:
            print(f"  [缺图] {name}")
            skip += 1
            continue
        if img.shape[:2] != (900, 1600):
            print(f"  [跳过: 尺寸 {img.shape[:2]}] {name}")
            skip += 1
            continue
        b._ocr_cache = {}
        b._ocr_cache_frame = img
        t = time.time()
        got = b._classify_page(img)
        d = time.time() - t
        good = got == exp
        ok += good
        bad += (not good)
        print(f"{'PASS' if good else 'FAIL'}  {name:44s} 期望={exp:11s} "
              f"实际={got:11s} ({d:5.1f}s)")
        if not good:
            detail.append(f"{name}: 期望 {exp}, 实际 {got}")
    print(f"\n{ok}/{ok + bad} 通过" + (f"  (跳过 {skip})" if skip else "")
          + ("\n不符明细:\n  " + "\n  ".join(detail) if detail else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
