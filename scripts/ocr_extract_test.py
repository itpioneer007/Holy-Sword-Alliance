"""端到端测试: 排行榜页面 OCR 解析 -> 写入打不过名单。

界面来源: 用户截屏 captures/arena_now.png (排行榜 Top 10)。
每行结构: "<区服号>区 <名字> <积分>"
目标: 把每个玩家写入 db 的 verdict='no' (用 record skip 语义)。
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot.db import OpponentDB  # noqa: E402
from rapidocr_onnxruntime import RapidOCR  # noqa: E402

# 排行榜左列在 1280x720 截图中的大致区域 (经验值, 后续可调整)
LEFT_REGION = (40, 110, 580, 660)  # x1, y1, x2, y2

ROW_PATTERN = re.compile(r"(\d+)\s*区\s+(\S+)\s+(\d+)")


def crop(img: np.ndarray, region: tuple[int, int, int, int]) -> np.ndarray:
    x1, y1, x2, y2 = region
    return img[y1:y2, x1:x2]


def imread_any(path: Path) -> np.ndarray:
    """绕过 cv2.imread 的中文路径 bug: numpy fromfile + imdecode。"""
    buf = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return img


def extract_players(img_path: Path) -> list[tuple[str, str, int]]:
    img = imread_any(img_path)
    roi = crop(img, LEFT_REGION)
    print(f"裁剪区域: {LEFT_REGION} -> 形状 {roi.shape}")
    ocr = RapidOCR()
    result, _ = ocr(roi)
    if not result:
        print("OCR 未识别到任何文字")
        return []

    # 按 y 坐标把 OCR 条目分行(容差 15 像素), 行内按 x 排序拼成完整文字
    def y_top(box):
        ys = []
        for pt in box:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2 and isinstance(pt[1], (int, float)):
                ys.append(pt[1])
        return min(ys) if ys else 0

    def x_left(box):
        xs = []
        for pt in box:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2 and isinstance(pt[0], (int, float)):
                xs.append(pt[0])
        return min(xs) if xs else 0

    raw = []
    for item in result:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        if not isinstance(box, (list, tuple)) or len(box) < 4:
            continue
        if not isinstance(text, str):
            continue
        t = text.strip()
        if not t:
            continue
        raw.append((box, t, score))

    items = sorted(raw, key=lambda it: (y_top(it[0]), x_left(it[0])))
    rows: list[list[tuple]] = []
    Y_TOL = 15
    for it in items:
        if rows and abs(y_top(it[0]) - y_top(rows[-1][0][0])) < Y_TOL:
            rows[-1].append(it)
        else:
            rows.append([it])
    print(f"\n=== 重组后行数: {len(rows)} ===")
    for row in rows:
        line = " ".join(it[1] for it in sorted(row, key=lambda r: x_left(r[0])))
        print(f"  y={y_top(row[0][0]):>5.0f}  {line}")

    # 行聚合后匹配 "区服号区 名字"; 积分因 OCR 漏掉不强求
    player_pat = re.compile(r"(\d+)\s*区\s+(\S+)")
    players: list[tuple[str, str, int]] = []
    print("\n=== 解析玩家 ===")
    for row in rows:
        line = " ".join(it[1] for it in sorted(row, key=lambda r: x_left(r[0])))
        m = player_pat.search(line)
        if not m:
            continue
        zone, name = m.group(1), m.group(2).strip()
        zone = f"{zone}区"
        key = (zone, name)
        if key in {(z, n) for z, n, _ in players}:
            continue
        players.append((zone, name, 0))
        print(f"  ✓ {zone} {name}")

    return players


def main() -> int:
    img_path = Path(__file__).resolve().parent.parent / "captures" / "arena_now.png"
    if not img_path.exists():
        print(f"截图不存在: {img_path}")
        return 1

    players = extract_players(img_path)
    if not players:
        print("\n未识别到任何玩家, 结束。")
        return 1

    # 用临时库做端到端测试, 不污染真实 data/sj_bot.db
    tmp = Path(tempfile.mkdtemp(prefix="sj_rank_test_"))
    db_path = tmp / "rank.db"
    db = OpponentDB(db_path)

    print("\n=== 写入打不过名单 (verdict=no) ===")
    for zone, name, score in players:
        # 排行榜读取 -> 建档; 把积分作为 rating 快照存
        db.upsert_from_scan(name=f"{zone} {name}", rating=score)
        # 用 record(skip) 让 verdict='no' 并记录 skip 次数
        db.record(name=f"{zone} {name}", result="skip")
        print(f"  → {zone} {name} 已写入打不过名单")

    # 导出名单文件
    out = db.export_lists(tmp / "lists")
    print("\n=== 导出文件 ===")
    for k, p in out.items():
        print(f"  {k}: {p}")
        print("----")
        print(p.read_text(encoding="utf-8"))
        print("----")

    # 统计验证
    st = db.stats()
    print(f"档案统计: {st}")
    assert st["cannot_beat"] >= len(players), "打不过名单写入数量不足"

    db.close()
    print("\nOK: OCR 解析 + 写入 + 导出 链路全通")
    return 0


if __name__ == "__main__":
    sys.exit(main())