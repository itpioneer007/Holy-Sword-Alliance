#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开箱奖励图标 —— 识别与模板库。

## 为什么需要这个模块

「宝箱奖励」弹窗**只画图标、不写物品名** (2026-09-29 用 4 张真实帧确认)。而用户要看
的是"这局拿到了什么" —— 光留一张图解决不了, 图得一张张翻。所以这里把图标认出来,
把**物品名**写进 `data/rewards.jsonl` 的 `item` 字段, 前端才能按名字展示与聚合。

## 物品名从哪来

不是 OCR, 也不是猜 —— 是 2026-09-29 拿 4 张真实帧与用户人工确认的口径对齐后固化的
映射 (用户原话: "紫色的是装备扫荡券 黄色的是副本扫荡券 绿色的是普通经验符文
蓝色的是钻石 还有一种紫色的卡片是紫色经验卡")。

## 判据: 为什么"颜色为主, 模板兜底"

先试过纯灰度模板匹配, 不够稳: 屏蔽角标后, 「紫卷轴 vs 紫卡片」仍拿 0.871, 而「金卷轴
vs 紫卷轴」拿 0.858 —— 同一物品自身的分数只有 1.000, 间隔仅 0.13, 余量太薄。
(原因见下: 这几种卡片是同一套底版, 只有图案本体不同, 灰度当然分不开。)

改用颜色后间隔拉开到两个数量级。实测占比 (取样区 = 图案本体):

    金卷轴(副本扫荡券)  黄 76.8%   紫 10.1%   银灰  0.1%
    紫卷轴(装备扫荡券)  黄  0.1%   紫 91.3%   银灰  0.2%
    紫卡片(紫色经验卡)  黄  3.7%   紫 79.6%   银灰 33.4%   <- 三张银灰卡牌
    钻石                绿 36.9%   青 35.7%   蓝 26.7%   ("蓝+青" 62.4%)

「紫卷轴 vs 紫卡片」的**银灰占比差 160 倍** (0.2% vs 33.4%) —— 这就是拿下这一对的
判据。模板匹配保留为**兜底**: 颜色说不清时 (新物品/光照异常) 才用, 且必须过阈值,
否则一律记「待命名」交给人工, 绝不猜。

## 两个坑 (都是实测踩出来的)

1. **卡片的底版是共用的**: 「金边框 + 紫内底」对金卷轴与紫卷轴**完全一样**, 只有图案
   本体颜色不同。所以取色区必须落在图案上, 取到边框/内底就分不开。
2. **右下角有「×N」数量角标**, 而且数量会变 (实测 ×2、×4)。不屏蔽的话, 同一物品换个
   数量自匹配会掉到 0.753 (低于阈值) —— 实测。所以匹配与统计前一律先涂掉那块。
"""
from __future__ import annotations

import pathlib
from typing import Optional

import cv2
import numpy as np

# ---------------------------------------------------------------- 几何常量

# 「宝箱奖励」弹窗中央大图标的 ROI (1600x900 全图绝对坐标)。
# 与 server.REWARD_CROPS["icon"] 是同一个位置 —— 那边负责"给人看", 这边负责"给机器认",
# 故意分成两份常量: 一个是展示裁剪、一个是识别裁剪, 将来各自动微调互不牵连。
REWARD_ICON_ROI = (690, 298, 882, 470)

# 「×N」数量角标固定在卡片右下角 (4 张真实帧观察一致), 但数量会变 (实测 ×2、×4)。
# 匹配/取色前把它涂黑: 角标是白字黑边 (低饱和), 留着会污染"银灰占比"这个关键判据。
# 坐标是 ROI 内的相对值 (x0, y0, x1, y1), 留了余量以覆盖 2 位数数量 (×10)。
REWARD_ICON_MASK = (122, 102, 186, 158)

# 颜色判据的取样区 (ROI 内相对坐标): **必须落在图案本体上** (见模块 docstring 坑 1)。
REWARD_HUE_ROI = (55, 50, 140, 125)

# ---------------------------------------------------------------- 判据阈值

# 颜色判据阈值。由 scripts/diag_reward_icons.py --hue 标定, 4 个真实样本上的余量都很大。
HUE_PURPLE_MIN = 50.0      # 紫 >= 50% -> 进紫色分支 (紫卷轴 91.3 / 紫卡 79.6)
HUE_SILVER_MIN = 15.0      # 紫色分支里 银灰 >= 15% -> 紫色经验卡 (33.4 vs 0.2, 差 160 倍)
HUE_BLUECYAN_MIN = 40.0    # 蓝+青 >= 40% -> 钻石 (62.4)
HUE_YELLOW_MIN = 50.0      # 黄 >= 50% -> 副本扫荡券 (76.8)
HUE_GREEN_MIN = 55.0       # 绿 >= 55% 且蓝青少 -> 普通经验符文 (暂无样本, 保守取值)

# 模板兜底阈值。交叉矩阵: 同类 1.000 / 异类最高 0.871 -> 取 0.90。
# 只在颜色判不出时才走模板, 所以可以取得偏严 (宁可"待命名", 不可猜错)。
REWARD_ICON_THRESHOLD = 0.90

# 模板库目录 (随代码入库, 与 captures/ 分开 —— 后者会被例行清理)
ICON_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "reward_icons"

# (模板文件名, 物品名, 主色标签, 图案说明)
# 模板缺失的条目会被自动跳过 (如绿色符文暂无样本), 不影响其它条目。
REWARD_ICON_SPECS: list[tuple[str, str, str, str]] = [
    ("dungeon_sweep.png",   "副本扫荡券",   "黄", "金色卷轴, 中间一个「扫」字"),
    ("equip_sweep.png",     "装备扫荡券",   "紫", "紫色卷轴, 中间一个「扫」字"),
    ("exp_card_purple.png", "紫色经验卡",   "紫", "紫色叠放卡牌 + 紫色药水瓶"),
    ("diamond.png",         "钻石",         "蓝", "蓝青色六边形宝石, 绿色底"),
    ("rune_exp_green.png",  "普通经验符文", "绿", "绿色符文 (暂无样本, 补进模板即生效)"),
]

UNKNOWN_NAME = "待命名"

# 色相分带 (OpenCV H: 0~179)
_HUE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("红", 0, 11), ("黄", 11, 41), ("绿", 41, 86), ("青", 86, 100),
    ("蓝", 100, 126), ("紫", 126, 156), ("洋红", 156, 180),
)

_template_cache: Optional[list[tuple[str, str, np.ndarray]]] = None


# ---------------------------------------------------------------- 基础工具

def crop_icon(img: np.ndarray) -> Optional[np.ndarray]:
    """从整帧里裁出弹窗中央图标。帧太小时返回 None (不回退整帧 —— 认错比认不出更糟)。"""
    if img is None:
        return None
    h, w = img.shape[:2]
    x0, y0, x1, y1 = REWARD_ICON_ROI
    if w < x1 or h < y1:
        return None
    return img[y0:y1, x0:x1]


def apply_mask(roi: np.ndarray) -> np.ndarray:
    """把右下角的「×N」数量角标涂黑 (返回副本)。

    模板与现场帧都要涂 —— 只涂一边等于人为制造差异。
    """
    out = roi.copy()
    x0, y0, x1, y1 = REWARD_ICON_MASK
    h, w = out.shape[:2]
    out[min(y0, h):min(y1, h), min(x0, w):min(x1, w)] = 0
    return out


def hue_shares(roi: np.ndarray) -> dict[str, float]:
    """统计取样区里各色相占比 (%), 外加一个特殊的 `银灰`。

    `银灰` = 低饱和 + 高亮 (S<70 & V>110), 专门用来抓「紫色经验卡上那三张银灰卡牌」。
    只统计有颜色的像素作分母 (饱和度与明度都过线), 否则黑色描边与背景金色放射会淹掉结果。
    """
    x0, y0, x1, y1 = REWARD_HUE_ROI
    sub = apply_mask(roi)[y0:y1, x0:x1]
    if sub.size == 0:
        return {"银灰": 0.0, **{n: 0.0 for n, _lo, _hi in _HUE_BANDS}}
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    flat = hsv.reshape(-1, 3)
    h = flat[:, 0].astype(int)
    s = flat[:, 1].astype(int)
    v = flat[:, 2].astype(int)
    colored = (s > 80) & (v > 70)
    n = max(1, int(colored.sum()))
    hh = h[colored]
    out = {name: 100.0 * int(((hh >= lo) & (hh < hi)).sum()) / n for name, lo, hi in _HUE_BANDS}
    total = max(1, sub.shape[0] * sub.shape[1])
    out["银灰"] = 100.0 * int(((s < 70) & (v > 110)).sum()) / total
    return out


# ---------------------------------------------------------------- 颜色判据 (主)

def classify_by_color(roi: np.ndarray) -> tuple[Optional[str], float, dict[str, float]]:
    """按**颜色口径**判物品。返回 (物品名 或 None, 置信度, 色相占比明细)。

    判定顺序即优先级。紫分支必须在最前 —— 钻石卡片的底是绿的, 而绿色符文也是绿的,
    所以"绿"这条规则反而要放在最后、并且要求蓝青都少, 否则会把钻石吃成符文。
    """
    sh = hue_shares(roi)
    purple = sh["紫"]
    silver = sh["银灰"]
    bluecyan = sh["蓝"] + sh["青"]
    yellow = sh["黄"]
    green = sh["绿"]

    if purple >= HUE_PURPLE_MIN:
        if silver >= HUE_SILVER_MIN:
            return "紫色经验卡", purple / 100.0, sh
        return "装备扫荡券", purple / 100.0, sh
    if bluecyan >= HUE_BLUECYAN_MIN:
        return "钻石", bluecyan / 100.0, sh
    if yellow >= HUE_YELLOW_MIN:
        return "副本扫荡券", yellow / 100.0, sh
    if green >= HUE_GREEN_MIN and bluecyan < 25.0:
        return "普通经验符文", green / 100.0, sh
    return None, 0.0, sh


# ---------------------------------------------------------------- 模板判据 (兜底)

def load_templates(force: bool = False) -> list[tuple[str, str, np.ndarray]]:
    """装载模板库 -> [(物品名, 主色标签, 灰度模板)]。缓存一次, `force=True` 强制重载。"""
    global _template_cache
    if _template_cache is not None and not force:
        return _template_cache

    items: list[tuple[str, str, np.ndarray]] = []
    for fn, name, color, _desc in REWARD_ICON_SPECS:
        p = ICON_DIR / fn
        if not p.exists():
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)  # 中文路径必须 fromfile
        if img is None:
            continue
        # 模板也屏蔽角标: 与现场帧保持同一套预处理, 否则等于自己给自己制造差异
        items.append((name, color, cv2.cvtColor(apply_mask(img), cv2.COLOR_BGR2GRAY)))
    _template_cache = items
    return items


def match_by_template(roi: np.ndarray, threshold: float = REWARD_ICON_THRESHOLD
                      ) -> tuple[str, float]:
    """灰度模板匹配 (兜底判据)。返回 (物品名 或 待命名, 最高得分)。

    只跑单一尺度: 实测同一素材在不同帧上像素级一致, 多尺度纯属浪费 CPU ——
    本机模板/OCR 都慢, 每局多算一轮不值得。
    """
    items = load_templates()
    if not items:
        return UNKNOWN_NAME, 0.0
    g = cv2.cvtColor(apply_mask(roi), cv2.COLOR_BGR2GRAY)
    best_score, best_name = -1.0, UNKNOWN_NAME
    for name, _color, tg in items:
        if tg.shape[0] > g.shape[0] or tg.shape[1] > g.shape[1]:
            continue
        v = float(cv2.matchTemplate(g, tg, cv2.TM_CCOEFF_NORMED).max())
        if v > best_score:
            best_score, best_name = v, name
    if best_score >= threshold:
        return best_name, best_score
    return UNKNOWN_NAME, max(best_score, 0.0)


# ---------------------------------------------------------------- 对外入口

def match_icon(img: np.ndarray, use_template_fallback: bool = True) -> tuple[str, float, str]:
    """认这枚图标是什么。入参 = **整帧**。返回 (物品名, 置信度, 判据来源)。

    判据来源取值: `颜色` / `模板` / `无`。
    """
    x0, y0, x1, y1 = REWARD_ICON_ROI
    if img is None:
        return UNKNOWN_NAME, 0.0, "无"
    h, w = img.shape[:2]
    if w < x1 or h < y1:
        return UNKNOWN_NAME, 0.0, "无"
    return match_roi(img[y0:y1, x0:x1], use_template_fallback=use_template_fallback)


def match_roi(roi: np.ndarray, use_template_fallback: bool = True
              ) -> tuple[str, float, str]:
    """入参 = **已裁好的 ROI** (诊断脚本与自测用)。判据与 match_icon 完全一致。"""
    if roi is None or roi.size == 0:
        return UNKNOWN_NAME, 0.0, "无"

    name, conf, _sh = classify_by_color(roi)
    if name:
        return name, conf, "颜色"

    if use_template_fallback:
        tname, tscore = match_by_template(roi)
        if tname != UNKNOWN_NAME:
            return tname, tscore, "模板"
        return UNKNOWN_NAME, tscore, "无"
    return UNKNOWN_NAME, 0.0, "无"


def dump_unknown(roi: np.ndarray, out_dir: pathlib.Path, stamp: str) -> Optional[str]:
    """把认不出的图标存下来, 供 `scripts/label_reward_icon.py` 人工命名。

    这是"待命名"闭环的一半: 认不出不猜, 但要让用户能**看图补名**, 补进模板库后
    历史记录也能用 label_reward_icon.py --backfill 回填。
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{stamp}.png"
        cv2.imencode(".png", roi)[1].tofile(str(p))     # 中文路径必须走 imencode().tofile()
        return p.name
    except Exception:
        return None


def name_of(item: Optional[str]) -> str:
    """给前端/日志用的展示名 —— None 与空串都归到「待命名」。"""
    return (item or "").strip() or UNKNOWN_NAME
