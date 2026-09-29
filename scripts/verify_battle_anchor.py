# -*- coding: utf-8 -*-
"""验证战斗页组合判据在正/负样本上的分离度, 并校验结算链优先级.

判据候选:
  A) skip_btn.png 模板命中 (SKIP_ROI)
  B) x2_btn.png   模板命中 (X2_ROI)
  C) SKIP_ROI OCR 文本含 "跳过" 或 "x2"
新方案 = (A and B) or C

⚠️ 重要: 宝箱结算层**叠加在战斗画面上** (rank_3_reward.png 上 x2=1.000 / skip=0.868),
故该页 `_is_in_battle` 为真**是设计使然**, 由管线「结算链优先于战斗页」兜住
⇒ 结算页**不能**当战斗判据的负样本, 必须按"是否被 _detect_settle_stage 识别"单独校验.

用法: python scripts/verify_battle_anchor.py
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot import rank_layout as rk          # noqa: E402
from sj_bot.battler import Battler            # noqa: E402

X2_ROI = getattr(rk, "X2_ROI", (1250, 720, 1470, 900))
THR = 0.65

# 只用固定异常图作样本: live_*.png 是实时截图, 会被后续运行覆盖, 不适合当基准
POS = ["anomaly_111324_not_rank_hub.png", "anomaly_075847_unknown_screen.png",
       "anomaly_075755_unknown_screen.png", "anomaly_075821_unknown_screen.png"]
NEG = ["rank_hub_clean.png", "rank_0_hub.png", "calib_11_rank_hub.png",
       "anomaly_112956_unknown_page_end.png",
       "anomaly_111142_main_city_rescue2.png", "anomaly_130244_main_city_rescue1.png",
       "anomaly_160008_rank_hub_failed.png"]
# 结算层叠加在战斗画面上 -> 走管线应判为结算 (优先), 不判为战斗
SETTLE = ["rank_3_reward.png"]


def tpl(img, fname, roi):
    t = cv2.cvtColor(cv2.imdecode(np.fromfile(str(ROOT / "assets" / fname),
                                              dtype=np.uint8),
                                  cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = roi
    reg = g[y0:y1, x0:x1]
    if reg.shape[0] < t.shape[0] or reg.shape[1] < t.shape[1]:
        return -1.0
    return float(cv2.minMaxLoc(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED))[1])


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    print(f"{'页面':<40}{'skip':>8}{'x2':>8}  {'A&B':>5}  {'C(OCR)':>7}  "
          f"{'战斗判据':>7}  {'结算链':>7}  期望")
    ok_all = True
    for tag, names, expect in (("POS", POS, True), ("NEG", NEG, False)):
        for n in names:
            p = ROOT / "captures" / n
            if not p.exists():
                print(f"  [skip] {n} 不存在")
                continue
            img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
            s = tpl(img, "skip_btn.png", rk.SKIP_ROI)
            x = tpl(img, "x2_btn.png", X2_ROI)
            a_and_b = (s >= THR) and (x >= THR)
            texts = [it["text"].lower() for it in b._ocr_texts(img, roi=rk.SKIP_ROI, scale=1.0)]
            c = any(("跳过" in t) or ("x2" in t) for t in texts)
            verdict = a_and_b or c
            st = b._detect_settle_stage(img)
            # 管线最终判定: 结算链优先于战斗页
            eff = "settle" if st else ("battle" if verdict else "other")
            want = "battle" if expect else "other"
            ok = eff == want
            ok_all &= ok
            print(f"{tag} {n[:36]:<36}{s:>8.3f}{x:>8.3f}  {str(a_and_b):>5}  {str(c):>7}"
                  f"  {str(verdict):>7}  {str(st or '-'):>7}  {want:<7}"
                  f"{'OK' if ok else '★FAIL★'}")
    for n in SETTLE:
        p = ROOT / "captures" / n
        if not p.exists():
            print(f"  [skip] {n} 不存在 (结算页样本)")
            continue
        img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
        s = tpl(img, "skip_btn.png", rk.SKIP_ROI)
        x = tpl(img, "x2_btn.png", X2_ROI)
        a_and_b = (s >= THR) and (x >= THR)
        texts = [it["text"].lower() for it in b._ocr_texts(img, roi=rk.SKIP_ROI, scale=1.0)]
        c = any(("跳过" in t) or ("x2" in t) for t in texts)
        verdict = a_and_b or c
        st = b._detect_settle_stage(img)
        eff = "settle" if st else ("battle" if verdict else "other")
        ok = eff == "settle"
        ok_all &= ok
        print(f"SETTLE {n[:33]:<33}{s:>8.3f}{x:>8.3f}  {str(a_and_b):>5}  {str(c):>7}"
              f"  {str(verdict):>7}  {str(st or '-'):>7}  {'settle':<7}"
              f"{'OK' if ok else '★FAIL★'}")
    print("\n" + ("全部通过" if ok_all else "存在不符样本, 判据需调整"))


if __name__ == "__main__":
    main()
