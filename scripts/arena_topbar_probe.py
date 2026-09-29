"""探针: OCR 全民争霸页顶栏 (我的积分 / 当前行动力 / 行动力恢复)。

用途: 标定"行动力"读数的 ROI 与 OCR 可靠性 (数字能否稳定识别)。
用法: python scripts/arena_topbar_probe.py captures/xxx.png
"""
import sys

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR


def main() -> None:
    p = sys.argv[1]
    img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"无法读取 {p}")
    print("image size:", img.shape)

    ocr = RapidOCR()
    res, _ = ocr(img)
    print("---- 全图 OCR (按 y 排序) ----")
    items = []
    for box, text, score in (res or []):
        try:
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            sc = float(score)
        except Exception:
            continue
        items.append((min(ys), min(xs), max(xs), max(ys), sc, str(text)))
    for y0, x0, x1, y1, sc, text in sorted(items):
        print(f"y={y0:6.0f}-{y1:6.0f} x={x0:6.0f}-{x1:6.0f} s={sc:.2f} | {text}")

    print("---- 顶栏区域 (y < 90) ----")
    for y0, x0, x1, y1, sc, text in sorted(items):
        if y0 < 90:
            print(f"  x={x0:6.0f}-{x1:6.0f} y={y0:5.0f}-{y1:5.0f} s={sc:.2f} | {text}")


if __name__ == "__main__":
    main()
