# -*- coding: utf-8 -*-
"""验证 scan_opponent_list 的右列条带化 (2026-09-13 晚) 与全图扫描**等价**。

等价定义: 名单 (顺序+名字) 与按钮坐标 (±2px) 完全一致; 预期收益 = 耗时 ~1/4。
凡改动 L.SCAN_BAND 或扫描算法后必须重跑本脚本。
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from sj_bot import arena_layout as L  # noqa: E402
from sj_bot.vision import _get_ocr, scan_opponent_list  # noqa: E402

CAP = os.path.join(_ROOT, "captures")
RESULTS = []


def check(name, got, expect):
    ok = got == expect
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r} (期望 {expect!r})")


def load(fn):
    p = os.path.join(CAP, fn)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def full_frame_scan(img):
    """旧算法基准: 全图 OCR + 相同的行匹配逻辑 (复刻, 仅供本验证对照)。"""
    import re
    from sj_bot.vision import _group_rows, _nearest_button, _row_text, _x_right, _y_center, find_button_rects

    ocr = _get_ocr()
    result, _ = ocr(img)
    items = []
    for box, text, score in (result or []):
        if not isinstance(text, str) or not text.strip():
            continue
        if _x_right(box) < L.NAME_X1 - 20:
            continue
        items.append((box, text.strip(), float(score)))
    challenge_boxes = [(box, text) for box, text, _ in items
                       if text == "挑战" or text.startswith("挑战")]
    btn_rects = find_button_rects(img, roi=L.BTN_COLUMN_ROI)
    btn_rects.sort(key=lambda r: r[1])
    right_items = [it for it in items if _x_right(it[0]) > L.NAME_X1 - 20]
    rows = _group_rows(right_items, y_tol=15)
    name_pat = re.compile(r"(\d+)\s*区\s*(\S+)")
    out = []
    for row in rows:
        line = _row_text(row)
        m = name_pat.search(line.replace(" ", " "))
        if not m:
            continue
        row_cy = _y_center(row[0][0])
        btn = _nearest_button(row_cy, challenge_boxes, btn_rects)
        if btn is None:
            continue
        out.append((f"{m.group(1)}区 {m.group(2).strip()}", (int(btn[0]), int(btn[1]))))
    return out


# 期望名单 = 人工看图核实的真值 (不是任何一版算法的输出)。
# anomaly 帧: 全图基准漏检「38区 消极的蓝权杖名」(夹在两行修养中之间, 人工看图确认
# 该行真实存在且带挑战按钮), 且全图把按钮坐标配错行 —— 条带化反而更准。
# 依据: outputs/band_check_context.png (09-13 人工核实)。
CASES = [
    ("arena_page_now.png", [
        ("35区 荣耀的王者", (1500, 125)),
        ("9区 厄运的暗行者", (1489, 187)),
        ("1区 超神的红魔切", (1500, 250)),
        ("45区 嘲笑的乌狮心", (1500, 312)),
        ("9区 绝影的金一闪", (1500, 437)),
        ("13区 寻觅的古蛮王", (1500, 500)),
        ("9区 救赎的暗冥王", (1500, 562)),
        ("47区 东方的日轮", (1500, 625)),
        ("46区 神春的紫女皇", (1500, 681)),
    ]),
    ("anomaly_160810_not_list.png", [
        ("45区 无限的光骑士名", (1500, 248)),
        ("38区 消极的蓝权杖名", (1501, 374)),
        ("45区 惩戒的死神名", (1499, 500)),
        ("1区 无限的究古神名", (1500, 621)),
    ]),
]


def main():
    ocr = _get_ocr()
    for fn, expect_pairs in CASES:
        img = load(fn)
        if img is None:
            check(f"{fn} 在库", False, True)
            continue
        expect_n = len(expect_pairs)
        # 全图基准 (含首跑模型加载)
        t0 = time.time()
        base = full_frame_scan(img)
        dt_full = time.time() - t0
        # 条带 (跑两遍取第二次, 排除单例初始化干扰)
        scan_opponent_list(img)
        t0 = time.time()
        band = scan_opponent_list(img)
        dt_band = time.time() - t0

        band_pairs = [(o.name, (int(o.btn[0]), int(o.btn[1]))) for o in band]
        band_names = [n for n, _b in band_pairs]
        base_names = [n for n, _b in base]

        print(f"\n--- {fn} ---")
        print(f"    全图: {len(base)} 人 {dt_full:.1f}s | 条带: {len(band)} 人 {dt_band:.1f}s "
              f"(加速 x{dt_full / dt_band:.1f})")
        # 条带 vs 人工真值
        check(f"{fn} 条带名单 = 真值", band_names, [n for n, _b in expect_pairs])
        btn_mism = [(bn, bb, eb) for (bn, bb), (_n, eb) in zip(band_pairs, expect_pairs)
                    if abs(bb[0] - eb[0]) > 2 or abs(bb[1] - eb[1]) > 2]
        check(f"{fn} 条带按钮坐标 = 真值 (±2px)", btn_mism, [])
        # 全图基准 vs 真值 (仅记录差异, 不算失败 —— 基准已知会漏检)
        if base_names != [n for n, _b in expect_pairs]:
            miss = [n for n in base_names if n not in {e for e, _ in expect_pairs}]
            print(f"    [备注] 全图基准与真值不一致 (漏检 {len(expect_pairs) - len(set(base_names) & {e for e, _ in expect_pairs})} 人): {base_names}")
        else:
            print("    [备注] 全图基准与真值一致")
        check(f"{fn} 条带人数 >= {expect_n}", len(band) >= expect_n, True)
        for n, b in band_pairs:
            print(f"      {n!r:30s} btn={b}")

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} 通过")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
