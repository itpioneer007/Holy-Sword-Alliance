"""解析 captures/stat_page_NN.png 的数字列 (发起挑战/被挑战/胜场数/败场数)。

名字列是艺术字, 本地 OCR 不可用(实测 恻->侧), 只自动读数字, 名字另行视觉直读。
列宽实测 (1600x900): 名字 393..530 | 发起挑战 640..720 | 被挑战 800..880 |
                     胜场数 955..1035 | 败场数 1095..1175
行中心: 256 + 52.6*i, i=0..9
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROWS_Y = [256, 309, 361, 414, 466, 519, 571, 624, 676, 729]
COLS = {
    "atk": (630, 730),
    "dfd": (790, 890),
    "win": (945, 1045),
    "lose": (1085, 1185),
}
_OCR = [None]


def _eng():
    from rapidocr_onnxruntime import RapidOCR
    if _OCR[0] is None:
        _OCR[0] = RapidOCR()
    return _OCR[0]


def read_cell(png: Path, box: tuple, yc: int) -> str:
    import numpy as np
    from PIL import Image

    im = Image.open(png).crop((box[0], yc - 26, box[1], yc + 26))
    im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
    res, _ = _eng()(np.array(im))
    return "".join(t[1] for t in res).strip() if res else ""


def to_int(s: str) -> int | None:
    d = re.sub(r"\D", "", s)
    return int(d) if d else None


def parse(png: Path):
    out = []
    for i, yc in enumerate(ROWS_Y):
        vals = {k: read_cell(png, box, yc) for k, box in COLS.items()}
        nums = {k: to_int(v) for k, v in vals.items()}
        out.append((i, vals, nums))
    return out


def main() -> None:
    fs = sorted(glob.glob(str(ROOT / "captures" / "stat_page_*.png")))
    total, withloss, nomatch = 0, 0, 0
    for f in fs:
        p = Path(f)
        rows = parse(p)
        line = []
        for i, vals, nums in rows:
            if all(v is None for v in nums.values()):
                continue
            total += 1
            lose = nums["lose"]
            if lose:
                withloss += 1
            if nums["atk"] != nums["dfd"] or nums["win"] != nums["lose"]:
                pass
            a, d, w, l = nums["atk"], nums["dfd"], nums["win"], nums["lose"]
            if None not in (a, d, w, l) and a + d != w + l:
                nomatch += 1
                line.append(f"r{i}:{a}/{d}/{w}/{l}!!")
            else:
                line.append(f"r{i}:{a}/{d}/{w}/{l}")
        print(p.name, " ".join(line))
    print(f"\n有数据行 {total} | 其中我败>=1 的行 {withloss} | 行内不自洽 {nomatch}")


if __name__ == "__main__":
    main()
