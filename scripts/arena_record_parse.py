"""解析「挑战记录」页截图 -> 结构化对局行 (单元格 OCR 版)。

面板几何 (1600x900 实测, rec_page_00):
  表头 y≈203; 10 行数据 y 中心 = 256 + 52.6*i
  列 x: 时间 405-550 | 挑战方 575-850 | 胜负 940-1030 | 防守方 1026-1258
胜负列是**挑战方视角**: 胜 = 挑战方赢, 负 = 挑战方输。

整行 OCR 会串行(区号与名字粘连、错字), 故按单元格裁切 + 4x 放大后分别识别。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CAP = ROOT / "captures"

ROW_Y0 = 256.0        # 第 1 行文字中心
ROW_DY = 52.6         # 行距
ROW_HALF = 23         # 行带半高
N_ROWS = 10
COLS = {              # 列名 -> (x0, x1)
    "time": (402, 552),
    "atk": (572, 852),
    "res": (938, 1032),
    "dfd": (1026, 1260),
}
SELF_NAME = "狂热的绯少女"
SELF_ZONE = "38区"

TIME_PAT = re.compile(r"(\d{2})\s*[/／]\s*(\d{2})\s*(\d{2})\s*[:：]\s*(\d{2})")
# OCR 常见近字纠错 (艺术粗体: 绯->排/维, 斩->析, 焰->陷 ... 逐步补)
CHAR_FIX = {
    "排": "绯", "菲": "绯", "维": "绯", "绊": "绯",
    "析": "斩", "渐": "斩", "侨": "斩",
    "寒": "赛", "塞": "赛", "察": "赛",
    "曝": "暗", "岸": "暗",
}
_ENGINE = [None]


def _ocr_engine():
    if _ENGINE[0] is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE[0] = RapidOCR()
    return _ENGINE[0]


def _ocr_cell(img: Image.Image, scale: int = 4) -> str:
    im = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    res, _ = _ocr_engine()(np.array(im))
    if not res:
        return ""
    toks = [t[1].strip() for t in res if isinstance(t[1], str) and t[1].strip()]
    return "".join(toks)


def _norm_name(raw: str) -> str:
    """'45区武神掘墓者' -> '45区 武神掘墓者'; 并做近字纠错。"""
    s = re.sub(r"\s+", "", raw)
    s = s.replace("V5", "").replace("VS", "")
    m = re.match(r"^(\d{1,3})[区K]?([\u4e00-\u9fffA-Za-z0-9]{2,14})$", s)
    if m:
        zone, name = m.group(1) + "区", m.group(2)
    else:
        m2 = re.match(r"^(\d{1,3})区", s)
        if m2:
            zone, name = m2.group(0), s[len(m2.group(0)):]
        else:
            zone, name = "", s
    name = "".join(CHAR_FIX.get(c, c) for c in name)
    return f"{zone} {name}".strip()


def parse_page(png: Path) -> list[dict]:
    im = Image.open(png)
    if im.width != 1600:
        im = im.resize((1600, 900), Image.LANCZOS)
    rows = []
    for i in range(N_ROWS):
        yc = ROW_Y0 + ROW_DY * i
        y0, y1 = int(yc - ROW_HALF), int(yc + ROW_HALF)
        cell_t = im.crop((COLS["time"][0], y0, COLS["time"][1], y1))
        cell_a = im.crop((COLS["atk"][0], y0, COLS["atk"][1], y1))
        cell_r = im.crop((COLS["res"][0], y0, COLS["res"][1], y1))
        cell_d = im.crop((COLS["dfd"][0], y0, COLS["dfd"][1], y1))
        t_raw = _ocr_cell(cell_t, 4)
        m = TIME_PAT.search(t_raw)
        a_raw = _ocr_cell(cell_a, 4)
        r_raw = _ocr_cell(cell_r, 6)
        d_raw = _ocr_cell(cell_d, 4)
        res = "胜" if "胜" in r_raw else ("负" if "负" in r_raw else "")
        rows.append({
            "row": i,
            "time": (f"09/{m.group(2)} {m.group(3)}:{m.group(4)}" if m else t_raw),
            "atk": _norm_name(a_raw),
            "res": res,
            "dfd": _norm_name(d_raw),
            "raw": {"t": t_raw, "a": a_raw, "r": r_raw, "d": d_raw},
        })
    return rows


def main() -> None:
    files = sorted(CAP.glob("rec_page_*.png"))
    files = [f for f in files if "_roi" not in f.name]
    files = [f for f in files if int(re.findall(r"\d+", f.name)[0]) <= 35]
    if len(sys.argv) > 1 and sys.argv[1] not in ("-", "--all"):
        files = [Path(sys.argv[1])]
    allrows = []
    for f in files:
        rows = parse_page(f)
        for r in rows:
            r["page"] = f.stem
        allrows.extend(rows)
        print(f"{f.name}: {len(rows)} 行 | 首行 {rows[0]['time']} | 末行 {rows[-1]['time']}")
    out = ROOT / "data" / "arena_records.json"
    out.write_text(json.dumps(allrows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("总行数", len(allrows), "->", out)


if __name__ == "__main__":
    main()
