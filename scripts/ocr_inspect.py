"""对截图跑 OCR 列坐标对照用: python ocr_inspect.py [截图路径]
不传参时默认 captures/rank_0_hub.png (兼容旧用法).
用法示例:
  python scripts/ocr_inspect.py captures/tmp/s3_single.png
"""
from __future__ import annotations
import sys
from pathlib import Path
from rapidocr_onnxruntime import RapidOCR

_DEFAULT = Path(r"C:/Users/19507/WorkBuddy/圣剑脚本挂机/captures/rank_0_hub.png")
IMG = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT
if not IMG.exists():
    print(f"!! 文件不存在: {IMG}")
    sys.exit(2)
ocr = RapidOCR()
res, _ = ocr(str(IMG))
print(f"=== OCR on {IMG.name} ===")
if not res:
    print("(no text)")
    sys.exit(0)
boxes = []
for box, text, score in res:
    if not (isinstance(text, str) and text.strip()):
        continue
    xs = [p[0] for p in box]; ys = [p[1] for p in box]
    cx, cy = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
    boxes.append((text.strip(), round(cx,1), round(cy,1), round(float(score),2)))
for t, cx, cy, sc in sorted(boxes, key=lambda b: (b[2], b[1])):
    print(f"  {sc:.2f}  ({cx:6.1f},{cy:6.1f})  {t}")
