# -*- coding: utf-8 -*-
"""诊断: 战斗页 '跳过' 按钮模板匹配失败原因.
输出: 截图尺寸 / 模板尺寸 / SKIP_ROI 内最高分 / 全图最高分及坐标.
用法: python scripts/diag_skip_probe.py [图片路径]  (不给路径则实时截图)
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sj_bot import rank_layout as rk  # noqa: E402

ADB = "D:/SoftwareDownload/platform-tools/adb.exe"
SERIAL = "127.0.0.1:16384"


def shot() -> np.ndarray:
    import subprocess
    import tempfile
    subprocess.run([ADB, "connect", SERIAL], capture_output=True, timeout=10)
    tmp = Path(tempfile.gettempdir()) / "diag_skip.png"
    with open(tmp, "wb") as fh:
        p = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=30)
        fh.write(p.stdout)
    return cv2.imdecode(np.fromfile(str(tmp), dtype=np.uint8), cv2.IMREAD_COLOR)


def main() -> None:
    if len(sys.argv) > 1:
        img = cv2.imdecode(np.fromfile(sys.argv[1], dtype=np.uint8), cv2.IMREAD_COLOR)
        print(f"[img] file = {sys.argv[1]}")
    else:
        img = shot()
        out = ROOT / "captures" / "diag_skip_probe.png"
        cv2.imwrite(str(out), img)
        print(f"[img] live shot -> {out}")
    print(f"[img] shape = {img.shape}  (h, w, c)")

    tpl_p = ROOT / "assets" / "skip_btn.png"
    tpl = cv2.cvtColor(cv2.imdecode(np.fromfile(str(tpl_p), dtype=np.uint8),
                                    cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
    print(f"[tpl] skip_btn.png shape = {tpl.shape}")

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = rk.SKIP_ROI
    print(f"[roi] SKIP_ROI = {(x0, y0, x1, y1)}  -> size {(x1-x0)}x{(y1-y0)}")
    region = g[y0:y1, x0:x1]
    r = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(r)
    print(f"[roi] best score = {mv:.4f} @ roi-local ({ml[0]},{ml[1]})"
          f" -> abs ({x0+ml[0]+tpl.shape[1]//2},{y0+ml[1]+tpl.shape[0]//2})")
    print(f"[roi] threshold  = 0.65  -> {'HIT' if mv >= 0.65 else 'MISS'}")

    rg = cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED)
    _, gv, _, gl = cv2.minMaxLoc(rg)
    print(f"[all] global best = {gv:.4f} @ abs ({gl[0]+tpl.shape[1]//2},"
          f"{gl[1]+tpl.shape[0]//2})")

    # 多尺度: 模板可能因分辨率缩放而不匹配
    for s in (0.8, 0.9, 1.0, 1.1, 1.25, 1.5):
        t2 = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
        if t2.shape[0] > region.shape[0] or t2.shape[1] > region.shape[1]:
            print(f"[scale {s:>4}] template larger than roi, skip")
            continue
        r2 = cv2.matchTemplate(region, t2, cv2.TM_CCOEFF_NORMED)
        _, mv2, _, ml2 = cv2.minMaxLoc(r2)
        print(f"[scale {s:>4}] roi best = {mv2:.4f} @ abs "
              f"({x0+ml2[0]+t2.shape[1]//2},{y0+ml2[1]+t2.shape[0]//2})")


if __name__ == "__main__":
    main()
