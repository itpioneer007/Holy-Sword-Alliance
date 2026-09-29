"""翻页探针: 在挑战记录面板上逐点尝试点击左右箭头, OCR 读取分页器文字。

用途: 定位 "1/1" 分页器两侧菱形箭头的可点区域。装饰性按钮, 命中区可能偏离视觉中心。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADB = r"D:\SoftwareDownload\platform-tools\adb.exe"
SERIAL = "127.0.0.1:16384"
CAP = ROOT / "captures"
PAGER_ROI = (1240, 700, 1500, 800)   # 分页器区域 (全图绝对坐标)


def adb(*args: str, timeout: int = 25) -> bytes:
    return subprocess.run(
        [ADB, "-s", SERIAL, *args], capture_output=True, timeout=timeout
    ).stdout


def shot(path: Path) -> None:
    path.write_bytes(adb("exec-out", "screencap", "-p"))


def ocr_pager(path: Path) -> str:
    from PIL import Image
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    im = Image.open(path).crop(PAGER_ROI)
    im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
    engine = RapidOCR()
    res, _ = engine(np.array(im))
    if not res:
        return "(无文字)"
    return " | ".join(t[1] for t in res)


def main() -> None:
    pts = [
        (1430, 744), (1445, 744), (1420, 730), (1420, 760), (1450, 750),
        (1265, 744), (1250, 744), (1280, 730), (1280, 760),
    ]
    if len(sys.argv) > 1 and sys.argv[1] == "left":
        pts = [(x, y) for x, y in pts if x < 1300]
    adb("connect", SERIAL)
    p = CAP / "pager_probe.png"
    shot(p)
    print("before:", ocr_pager(p))
    for x, y in pts:
        adb("shell", "input", "tap", str(x), str(y))
        time.sleep(1.1)
        shot(p)
        print(f"tap({x},{y}) ->", ocr_pager(p))


if __name__ == "__main__":
    main()
