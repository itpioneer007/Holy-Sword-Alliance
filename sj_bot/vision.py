"""视觉感知层 (V1: 实测于 2026-09-07 全民争霸对战列表)。

识别方案已定案: Unity 自绘 UI, 控件树拿不到 (登录界面 dump 仅 6 节点)。
本层主路径 = RapidOCR 识别文字 + OpenCV 颜色模板定位按钮, DeepSeek 截图兜底(预留)。

已实测能力 (captures/opponent_list.png 1600x900):
  - 右侧挑战列表: 每行"区服+玩家名" + 右侧同一竖线的"挑战"按钮
  - 玩家自己所在行无挑战按钮 (自己不能打自己)
  - 挑战按钮 = 蓝青渐变, x 中心 ~1499 固定, 行高 ~62px
  - 已知局限: 中文艺术字形近字 OCR 错字(千->干/芽->歼); 数字漏识别
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .config import Config
from . import arena_layout as L


def imread_any(path: str | Path) -> np.ndarray:
    """绕过 cv2.imread 的中文路径 bug: numpy fromfile + imdecode。"""
    buf = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return img


# ---------------------------------------------------------------- 异常帧去重 (2026-09-29)
# 起因: `Battler._save_anomaly` 每走到一次就把当前屏留下, 于是"导航重试 3 轮"= 3 张
# 画面几乎相同的图, 主城被遮挡反复自愈也把同一屏留十几次。实测 captures/ 顶层
# 66% (47.3MB / 71.7MB) 是同屏幕近重复 —— 这些帧彼此不携带新信息, 日志行才是"发生了几次".
#   · 运行时: battler 落图前与该进程内已存的帧比, 够像就跳过 (不额外扫盘);
#   · 事后: `scripts/cleanup_captures.py --dedup-anomaly` 按天合并历史重复帧。
ANOMALY_FP_SIZE = (32, 18)      # 指纹分辨率: 够区分页面, 又不受噪点/逐帧抖动影响
# 阈值标定 (2026-09-29, 18 张真实异常帧两两全比 + 合成扰动):
#   真实重复帧   (同屏重截 / 同一天不同时刻 / 跨天同屏) 实测 0.1 ~ 1.5
#   加 σ=16 高斯噪声                                     实测 0.58   (缩放把噪声平均掉了)
#   首张"确实不同"的帧对 (两张内容不同的黑屏)             实测 6.01
#   不同页面 (主城 vs 幸运大转盘)                        实测 > 20
# 取 2.5 = 把 1.5 与 6.01 的余量均分 —— **刻意偏低**:
#   漏合并只是多留一张图(无害), 而错合并会删掉真证据(不可逆)。宁可少删。
ANOMALY_DEDUP_DIFF = 2.5


def anomaly_fingerprint(img: "np.ndarray | None"):
    """异常帧的粗指纹 = 灰度 + 缩到 32x18。**只用来判"是不是同一画面"**, 不做识别。

    ⚠️ 不做均值归一化 (曾试过): 归一化能让"整体调亮/调暗"也判成同一屏, 但会让**所有
    纯色屏**(全黑加载页 / 全白闪屏) 归一化成同一个零矩阵而互相误并 —— 得不偿失。
    真重复帧的距离本来就 ≈0 (同一台机同一渲染重截), 不需要靠归一化去容忍亮度。
    """
    if img is None:
        return None
    try:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return cv2.resize(g, ANOMALY_FP_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32)
    except Exception:      # 畸形帧 (非 3 通道 / 0 尺寸) -> 拿不到指纹
        return None


def same_screen(a, b, diff: float = ANOMALY_DEDUP_DIFF) -> bool:
    """两个指纹是否同一画面。任一为空 => False, 即**保守地认为不同**(宁可多留图)。"""
    if a is None or b is None:
        return False
    try:
        return float(np.abs(a - b).mean()) < diff
    except Exception:
        return False


# ---------------------------------------------------------------- OCR 基础
def _y_top(box) -> float:
    ys = []
    for pt in box:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2 and isinstance(pt[1], (int, float)):
            ys.append(pt[1])
    return min(ys) if ys else 0.0


def _x_left(box) -> float:
    xs = []
    for pt in box:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2 and isinstance(pt[0], (int, float)):
            xs.append(pt[0])
    return min(xs) if xs else 0.0


def _x_right(box) -> float:
    xs = []
    for pt in box:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2 and isinstance(pt[0], (int, float)):
            xs.append(pt[0])
    return max(xs) if xs else 0.0


def _y_center(box) -> float:
    return (_y_top(box) + max(
        (p[1] for p in box if isinstance(p, (list, tuple)) and len(p) >= 2 and isinstance(p[1], (int, float))),
        default=_y_top(box),
    )) / 2


def _group_rows(items: list[tuple], y_tol: int = 15) -> list[list[tuple]]:
    """按 y 中心分行(容差), 行内不排序, 原样返回供调用方按 x 排。"""
    items = sorted(items, key=lambda it: _y_center(it[0]))
    rows: list[list[tuple]] = []
    for it in items:
        if rows and abs(_y_center(it[0]) - _y_center(rows[-1][0][0])) < y_tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    return rows


def _row_text(row: list[tuple]) -> str:
    """行内按 x 排序拼接文本(用于识别一行完整内容)。"""
    parts = [it[1] for it in sorted(row, key=lambda it: _x_left(it[0]))]
    return " ".join(parts)


# ---------------------------------------------------------------- 按钮定位
def find_button_rects(
    img: np.ndarray,
    b_min: int = L.BTN_MASK_B_MIN,
    br_gap: int = L.BTN_MASK_BR_GAP,
    min_w: int = 80,
    min_h: int = 30,
    roi: tuple[int, int, int, int] | None = None,
) -> list[tuple[int, int, int, int]]:
    """按蓝青渐变颜色找所有按钮矩形 (x1,y1,x2,y2)。

    挑战按钮主色: b>200 且 b-r>80 (BGR, 实测蓝青渐变)。
    返回按 y 排序的矩形列表。
    """
    if roi is not None:
        x0, y0, x1, y1 = roi
        img = img[y0:y1, x0:x1]
    else:
        x0 = y0 = 0
    b, g, r = (
        img[:, :, 0].astype(int),
        img[:, :, 1].astype(int),
        img[:, :, 2].astype(int),
    )
    mask = ((b > b_min) & ((b - r) > br_gap)).astype(np.uint8) * 255
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    rects = []
    for i in range(1, n):  # 0 是背景
        x, y, w, h, area = stats[i]
        if w < min_w or h < min_h or area < min_w * min_h * 0.5:
            continue
        rects.append((x0 + x, y0 + y, x0 + x + w, y0 + y + h))
    rects.sort(key=lambda r: r[1])  # 按 y 排序
    return rects


# ---------------------------------------------------------------- 列表扫描
@dataclass
class Opponent:
    """列表扫描到的一个对手(可挑战玩家)。"""

    name: str                # 原始 OCR 名字(可能含错字)
    zone: str = ""           # 区服号
    name_raw: str = ""       # 不含区服的玩家名
    cy: float = 0.0          # 该行 y 中心(用于计算按钮点击坐标)
    btn: tuple = (0, 0)      # 挑战按钮点击坐标 (x, y)


def normalize_name(name_raw: str, zone: str) -> str:
    """给纯玩家名加区服前缀 (db 里存的 canonical 名字格式)。"""
    name_raw = name_raw.strip()
    if not name_raw:
        return ""
    return f"{zone} {name_raw}" if zone else name_raw


# OCR 引擎单例 (2026-09-13): 旧写法 `RapidOCR()` 在**函数内每次调用新建** ——
# arena 主循环每次扫描都重载一遍 ONNX 模型 (秒级开销), 且用的是**默认 det 参数**
# (min+736 放大陷阱), 违反项目「OCR 一律走 battler.make_ocr()」的统一约定。
# 延迟导入避免与 battler 循环依赖 (battler._pick_target 是函数级 import 本模块)。
_OCR_ENGINE = None


def _get_ocr():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from sj_bot.battler import make_ocr
        _OCR_ENGINE = make_ocr()
    return _OCR_ENGINE


def scan_opponent_list(img: np.ndarray) -> list[Opponent]:
    """核心扫描: 从对战列表截图提取每个可挑战对手 + 按钮坐标。

    算法:
      1. RapidOCR 识别**右列条带** L.SCAN_BAND (2026-09-13 晚: 旧版 OCR 全图后按
         x>1102 过滤, 左侧 62% 面积白烧; 条带化后单次扫描 13~21s → 实测见
         scripts/verify_arena_band.py), box 坐标回移到全图坐标系;
      2. 每行拼接文本, 匹配 '^X区 名字' 提取 区服+玩家名;
      3. 行内找'挑战'关键字所在 x 区域, 与名字行配对;
      4. 用按钮矩形(蓝青 mask, 仍走全图+BTN_COLUMN_ROI)定位实际按钮,
         取每行对应的点击坐标;
      5. 无'挑战'的行 = 玩家自己 (或不可挑战), 跳过。
    """
    ocr = _get_ocr()
    bx0, by0, bx1, by1 = L.SCAN_BAND
    result, _ = ocr(img[by0:by1, bx0:bx1])
    if not result:
        return []

    # 条带坐标 -> 全图坐标, 再按右列过滤 (防御: 条带外的残留文本)
    items = []
    for box, text, score in result:
        if not isinstance(text, str) or not text.strip():
            continue
        box = [[p[0] + bx0, p[1] + by0] for p in box]
        if _x_right(box) < L.NAME_X1 - 20:  # 右侧区域之外(左侧战报/播报)
            continue
        items.append((box, text.strip(), float(score)))

    # 找所有"挑战"文字块 (作为每行锚点)
    challenge_boxes = [
        (box, text) for box, text, _ in items
        if text == "挑战" or text.startswith("挑战")
    ]
    if not challenge_boxes:
        return []

    # 找名字块(含区服的行)
    zone_pat = re.compile(r"^(\d+)\s*区\s*$|^(\d+)区")
    name_pat = re.compile(r"(\d+)\s*区\s*(\S+)")
    opponents: list[Opponent] = []

    # 按钮矩形辅助: 若颜色定位失败则回退到"挑战文字框中心"
    btn_rects = find_button_rects(img, roi=L.BTN_COLUMN_ROI)
    btn_rects.sort(key=lambda r: r[1])

    # 按行聚合后匹配: 因 OCR 常把 '47区' '隐身的白星辰' 拆成两块
    right_items = [it for it in items if _x_right(it[0]) > L.NAME_X1 - 20]
    rows = _group_rows(right_items, y_tol=15)
    for row in rows:
        line = _row_text(row)
        m = name_pat.search(line.replace(" ", " "))
        if not m:
            continue
        zone, name_raw = m.group(1), m.group(2).strip()
        row_cy = _y_center(row[0][0])
        # 找同行(±40px)挑战按钮; 无按钮 = 玩家自己/不可挑战, 跳过
        btn = _nearest_button(row_cy, challenge_boxes, btn_rects)
        if btn is None:
            continue
        opp = Opponent(
            name=normalize_name(name_raw, f"{zone}区"),
            zone=f"{zone}区",
            name_raw=name_raw,
            cy=row_cy,
            btn=btn,
        )
        opponents.append(opp)
    return opponents


def _nearest_button(
    row_cy: float,
    challenge_boxes: list[tuple],
    btn_rects: list[tuple[int, int, int, int]],
) -> tuple[int, int] | None:
    """找该行对应的挑战按钮点击坐标 (优先颜色矩形, 回退文字框中心)。"""
    best: tuple[int, int] | None = None
    best_dy = 1e9
    for rect in btn_rects:
        x1, y1, x2, y2 = rect
        bcy = (y1 + y2) / 2
        dy = abs(bcy - row_cy)
        if dy < 40 and dy < best_dy:
            best_dy = dy
            best = ((x1 + x2) // 2, int(bcy))
    if best is None:
        # 回退: 文字框中心
        for box, _text in challenge_boxes:
            bcy = _y_center(box)
            if abs(bcy - row_cy) < 40:
                return (int(_x_left(box) + (_x_right(box) - _x_left(box)) / 2), int(bcy))
    return best


def template_dir() -> Path:
    return Config().template_dir
