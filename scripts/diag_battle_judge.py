# -*- coding: utf-8 -*-
"""诊断: 战斗页判据的稳定性与可用特征.

对每张图输出:
  1) _is_in_battle 连续 3 次 (看是否稳定 — OCR 随机性)
  2) skip 模板分数 + SKIP_ROI 内 OCR 原文
  3) 左下角"自动战斗"OCR (战斗页常驻静态 UI, 候选第二判据)
  4) X2 按钮模板分数
用法: python scripts/diag_battle_judge.py [图 ...]
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot import rank_layout as rk          # noqa: E402
from sj_bot.battler import Battler            # noqa: E402

ADB = "D:/SoftwareDownload/platform-tools/adb.exe"
SERIAL = "127.0.0.1:16384"
# 左下角"自动战斗"按钮 (两行文字) 候选 ROI
AUTO_BATTLE_ROI = (20, 730, 240, 900)
AUTO_HINTS = ("自动", "战斗")


def load(p: str) -> np.ndarray:
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def shot() -> np.ndarray:
    import subprocess
    import tempfile
    subprocess.run([ADB, "connect", SERIAL], capture_output=True, timeout=10)
    p = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                       capture_output=True, timeout=30)
    f = Path(tempfile.gettempdir()) / "diag_judge.png"
    f.write_bytes(p.stdout)
    return cv2.imdecode(np.fromfile(str(f), dtype=np.uint8), cv2.IMREAD_COLOR)


def tpl_score(img: np.ndarray, fname: str, roi: tuple) -> float:
    """原始模板最高分 (不套阈值), 用于诊断."""
    tpl = cv2.cvtColor(cv2.imdecode(
        np.fromfile(str(ROOT / "assets" / fname), dtype=np.uint8),
        cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = roi
    reg = g[y0:y1, x0:x1]
    if reg.shape[0] < tpl.shape[0] or reg.shape[1] < tpl.shape[1]:
        return -1.0
    return float(cv2.minMaxLoc(cv2.matchTemplate(reg, tpl, cv2.TM_CCOEFF_NORMED))[1])


def probe(b: Battler, img: np.ndarray, name: str) -> None:
    print("=" * 78)
    print(f"[{name}]")
    tpl_hit = b._find_skip_btn(img)
    print(f"  skip 模板           : {tpl_hit}")

    print(f"  skip 模板最高分     : {tpl_score(img, 'skip_btn.png', rk.SKIP_ROI):.4f}"
          f"  (阈值 {b._BTN_TPL_MIN})")

    print(f"  X2 模板分           : {tpl_score(img, 'x2_btn.png', (1250, 720, 1470, 900)):.4f}"
          f"  (阈值 {b._BTN_TPL_MIN})")

    texts = [it["text"] for it in b._ocr_texts(img, roi=rk.SKIP_ROI, scale=1.0)]
    print(f"  SKIP_ROI OCR        : {texts}")

    vt = [it["text"] for it in b._ocr_texts(img, roi=rk.VERDICT_ROI, scale=1.0)]
    print(f"  顶部 VERDICT OCR    : {vt[:8]}")

    ab = [it["text"] for it in b._ocr_texts(img, roi=AUTO_BATTLE_ROI, scale=1.0)]
    print(f"  左下'自动战斗' OCR  : {ab}")

    res = [b._is_in_battle(img) for _ in range(3)]
    print(f"  _is_in_battle x3    : {res}   {'稳定' if len(set(res)) == 1 else '★不稳定★'}")


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    args = sys.argv[1:]
    if not args:
        img = shot()
        out = ROOT / "captures" / "diag_battle_judge_live.png"
        cv2.imencode(".png", img)[1].tofile(str(out))
        print(f"[live shot] -> {out}")
        probe(b, img, "LIVE")
        return
    for a in args:
        p = Path(a)
        if not p.is_absolute():
            p = ROOT / a
        if p.exists():
            probe(b, load(str(p)), p.name)


if __name__ == "__main__":
    main()
