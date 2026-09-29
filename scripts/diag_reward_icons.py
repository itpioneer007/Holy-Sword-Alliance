#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开箱奖励图标 —— 匹配器标定诊断。

背景: 「宝箱奖励」弹窗**不显示物品名文字** (2026-09-29 实测), 想知道"这局拿到了什么"
只能靠图标本身。本脚本用已经人工确认过名称的历史帧做**交叉匹配矩阵**, 回答两个问题:

  1. 同一枚图标在不同帧上的得分有多高?  -> 决定"命中阈值"
  2. 两枚**不同**图标互相匹配能拿多少分?  -> 决定"区分度", 阈值的上界

只有 1 与 2 之间留出足够宽的间隔, 模板匹配才是个确定性判据; 否则就是碰运气。

最危险的一对是「装备扫荡券」(紫卷轴) 与「紫色经验卡」(紫卡片) —— 色相几乎
一样 (紫 61% vs 43%), 按颜色分不开, 只能靠形状。这个脚本会重点打印它们的得分。

用法:
    python scripts/diag_reward_icons.py                # 用 data/rewards.jsonl 里全部样本
    python scripts/diag_reward_icons.py --dump         # 顺带把 ROI 裁图导出到 outputs/
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.reward_icons import (  # noqa: E402
    ICON_DIR, REWARD_ICON_MASK, REWARD_ICON_ROI, REWARD_ICON_SPECS,
    REWARD_ICON_THRESHOLD, apply_mask, load_templates, match_roi,
)

# 样本时间 -> 模板文件名。2026-09-29 拿这 4 帧与用户人工确认口径后固化的映射;
# 只在 --emit 时用, 生产路径不读 (生产读的是 assets/reward_icons/ 里已落盘的模板)。
EMIT_MAP: dict[str, str] = {
    "16:22": "dungeon_sweep.png",     # 金卷轴「扫」
    "16:24": "exp_card_purple.png",   # 紫色卡牌 + 药水瓶
    "16:38": "diamond.png",           # 蓝青宝石
    "16:40": "equip_sweep.png",       # 紫卷轴「扫」
}


def load_samples() -> list[tuple[dict, np.ndarray]]:
    """从 rewards.jsonl 读样本, 裁出图标 ROI。"""
    rp = ROOT / "data" / "rewards.jsonl"
    if not rp.exists():
        print("找不到 data/rewards.jsonl")
        return []
    out: list[tuple[dict, np.ndarray]] = []
    for line in rp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        p = ROOT / "captures" / rec.get("file", "")
        if not p.exists():
            continue
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        x0, y0, x1, y1 = REWARD_ICON_ROI
        out.append((rec, img[y0:y1, x0:x1].copy()))
    return out


def gray(a: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="导出 ROI 裁图到 outputs/")
    ap.add_argument("--emit", action="store_true",
                    help="按 EMIT_MAP 把样本图标写进 assets/reward_icons/ 当模板")
    ap.add_argument("--hue", action="store_true", help="跑色区探索 (颜色判据该从哪里取)")
    ap.add_argument("--scales", type=float, nargs="*", default=[1.0],
                    help="多尺度匹配的缩放因子, 例如 0.95 1.0 1.05")
    args = ap.parse_args()

    samples = load_samples()
    if not samples:
        print("没有可用样本")
        return 1

    print(f"样本 {len(samples)} 条, ROI={REWARD_ICON_ROI} "
          f"(尺寸 {REWARD_ICON_ROI[2] - REWARD_ICON_ROI[0]}x{REWARD_ICON_ROI[3] - REWARD_ICON_ROI[1]})")
    print()

    if args.dump:
        out = ROOT / "outputs" / "_rwicon"
        out.mkdir(parents=True, exist_ok=True)
        for rec, roi in samples:
            # 注意: ts[11:16] 是 "16:22", **含冒号** —— Windows 文件名非法,
            # tofile 会写出 0 字节的 "16" 而不报错 (2026-09-29 踩过)。必须去掉冒号。
            stamp = rec["ts"][11:19].replace(":", "")
            name = f"{stamp}_{rec.get('item') or rec.get('verdict', '?')}.png"
            big = cv2.resize(roi, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
            cv2.imencode(".png", big)[1].tofile(str(out / name))
        print(f"ROI 裁图已导出(3x放大): {out}")
        print()

    if args.emit:
        print("=== 生成模板库 (assets/reward_icons/) ===")
        ICON_DIR.mkdir(parents=True, exist_ok=True)
        ref_dir = ICON_DIR / "ref"
        ref_dir.mkdir(parents=True, exist_ok=True)
        cnt = 0
        for rec, roi in samples:
            key = rec["ts"][11:16]
            fn = EMIT_MAP.get(key)
            if not fn:
                continue
            p = ICON_DIR / fn
            # 落盘前先屏蔽角标: 模板里留着"×2"的话, 现场是"×4"就白瞎了
            cv2.imencode(".png", apply_mask(roi))[1].tofile(str(p))
            # 同时固化**未屏蔽**的原始 ROI 当回归样本。
            # 为什么不直接拿 captures/rewards 里的帧当样本: captures/ 有例行清理
            # (2026-09-23 nav_retry 自测就因样本被清而长期崩)。样本必须放 assets/。
            refp = ref_dir / f"{rec['ts'][11:19].replace(':', '')}_{rec['verdict']}.png"
            cv2.imencode(".png", roi)[1].tofile(str(refp))
            print(f"  写入 {fn:22s} <- {key} {rec['verdict']:4s}  "
                  f"{roi.shape[1]}x{roi.shape[0]}  模板 {p.stat().st_size / 1024:.0f}KB"
                  f" / 样本 {refp.stat().st_size / 1024:.0f}KB")
            cnt += 1
        load_templates(force=True)
        print(f"  共 {cnt} 个模板 + {cnt} 份回归样本\n")

    # ---- 1) 样本两两交叉得分 (自身 vs 他人), 均按生产预处理: 屏蔽右下角数量角标 ----
    print("=== 样本 x 样本 交叉得分 (屏蔽角标后, TM_CCOEFF_NORMED, 灰度) ===")
    head = " " * 10 + " ".join(f"{r['ts'][11:16]:>8s}" for r, _ in samples)
    print(head)
    diag: list[float] = []          # 对角线(自身)得分
    off: list[tuple[float, str]] = []  # 非对角线(异类)得分
    for i, (ri, ti) in enumerate(samples):
        tg = gray(apply_mask(ti))
        cells = []
        for j, (rj, tj) in enumerate(samples):
            sj = gray(apply_mask(tj))
            if sj.shape[0] > tg.shape[0] or sj.shape[1] > tg.shape[1]:
                sj = cv2.resize(sj, (tg.shape[1], tg.shape[0]))
            res = cv2.matchTemplate(tg, sj, cv2.TM_CCOEFF_NORMED)
            v = float(res.max())
            cells.append(v)
            if i == j:
                diag.append(v)
            else:
                off.append((v, f"{ri['ts'][11:16]} vs {rj['ts'][11:16]}"))
        tag = ri.get("item") or "?"
        print(f"{ri['ts'][11:16]:>8s}  " + " ".join(f"{x:8.3f}" for x in cells) + f"   {tag}")
    print()

    # ---- 1b) 角标扰动: 模拟"同一物品换了个数量" ----
    # 把 A 帧的角标区(含边框)整块换成 B 帧的 -> 变化比单纯数量变化更剧烈。
    # 比较"屏蔽前/后"的自匹配得分: 屏蔽后应当稳在高位, 否则阈值就得往上抬。
    print("=== 角标扰动鲁棒性 (同物品换数量/边框色) ===")
    if len(samples) >= 2:
        x0, y0, x1, y1 = REWARD_ICON_MASK
        print(f"  角标屏蔽区(ROI内) = ({x0},{y0})-({x1},{y1})")
        for i, (ri, ti) in enumerate(samples):
            donor = samples[(i + 1) % len(samples)][1]
            pert = ti.copy()
            pert[y0:y1, x0:x1] = donor[y0:y1, x0:x1]
            if args.dump:
                out = ROOT / "outputs" / "_rwicon"
                cv2.imencode(".png", cv2.resize(pert, None, fx=3, fy=3,
                                                interpolation=cv2.INTER_NEAREST)
                             )[1].tofile(str(out / f"{ri['ts'][11:19].replace(':','')}_perturbed.png"))
            raw = float(cv2.matchTemplate(gray(ti), gray(pert), cv2.TM_CCOEFF_NORMED).max())
            masked = float(cv2.matchTemplate(gray(apply_mask(ti)), gray(apply_mask(pert)),
                                             cv2.TM_CCOEFF_NORMED).max())
            verdict = "OK" if masked >= REWARD_ICON_THRESHOLD else "!! 低于阈值"
            print(f"  {ri['ts'][11:16]}  不屏蔽={raw:.3f}  屏蔽后={masked:.3f}   {verdict}")
    print()

    # ---- 1c) 色区探索: 颜色判据该从哪里取 ----
    # 卡片本身有"金边框 + 紫内底"的共同底版 (金卷轴与紫卷轴**底版完全相同**)，
    # 所以取色必须落在**图案本体**上，取到边框/内底就分不开。这里比较三个候选区域。
    if args.hue:
        cands = {
            "A 全卡片": (20, 20, 172, 152),
            "B 中央":   (55, 50, 140, 125),
            "C 核心":   (68, 58, 130, 118),
        }
        print("=== 色区探索 (相对 ROI 的子区域) ===")
        for cname, (cx0, cy0, cx1, cy1) in cands.items():
            print(f"-- {cname} ({cx0},{cy0})-({cx1},{cy1})")
            print("    样本      有色px   主色    红    黄    绿    青    蓝    紫   洋红   银灰%")
            for rec, roi in samples:
                sub = roi[cy0:cy1, cx0:cx1]
                hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV).reshape(-1, 3)
                h = hsv[:, 0].astype(int); s = hsv[:, 1].astype(int); v = hsv[:, 2].astype(int)
                colored = (s > 80) & (v > 70)
                n = max(1, int(colored.sum()))
                hh = h[colored]
                bands = (("红", 0, 11), ("黄", 11, 41), ("绿", 41, 86),
                         ("青", 86, 100), ("蓝", 100, 126), ("紫", 126, 156), ("洋红", 156, 180))
                pcts = [100.0 * int(((hh >= lo) & (hh < hi)).sum()) / n for _n, lo, hi in bands]
                silver = 100.0 * int(((s < 70) & (v > 110)).sum()) / max(1, sub.shape[0] * sub.shape[1])
                top = bands[max(range(len(bands)), key=lambda k: pcts[k])][0]
                print(f"    {rec['ts'][11:16]:>8s}  {n:6d}   {top:>4s}  "
                      + " ".join(f"{p:5.1f}" for p in pcts) + f"  {silver:5.1f}")
        print()
    # ---- 2) 走生产入口 match_roi (颜色为主, 模板兜底) ----
    print("=== 生产路径 match_roi ===")
    for rec, roi in samples:
        item, score, src = match_roi(roi)
        print(f"  {rec['ts'][11:16]}  {rec['verdict']:4s}  ->  {item:8s} "
              f"置信={score:.3f}  判据={src}")
    print()

    # ---- 3) 阈值标定 (颜色与模板各报一组) ----
    print("=== 阈值标定 ===")
    if off:
        worst_same = min(diag)
        best_diff = max(v for v, _ in off)
        print(f"  [模板] 同类最低(自身) {worst_same:.3f} / 异类最高 {best_diff:.3f} "
              f"/ 间隔 {worst_same - best_diff:.3f} (当前阈值 {REWARD_ICON_THRESHOLD})")
        if worst_same > best_diff:
            print(f"         理论中值 {(worst_same + best_diff) / 2:.2f}")
        else:
            print("         !! 间隔为负: 模板不足以单独定案 (所以颜色是主判据)")
        top = sorted(off, reverse=True)[:5]
        print("         最容易混淆的 5 对:")
        for v, k in top:
            print(f"           {v:.3f}  {k}")
    print("  [颜色] 各判据余量见 --hue 段; 关键一对是「紫卷轴 vs 紫卡」的银灰占比")
    print()

    # ---- 4) 模板库状态 ----
    print("=== 模板库 ===")
    have = {n for n, _c, _t in load_templates(force=True)}
    for fn, name, color, desc in REWARD_ICON_SPECS:
        p = ICON_DIR / fn
        mark = "OK " if p.exists() else "-- "
        print(f"  {mark}{name:8s} [{color}] {fn:22s} {desc}")
    missing = [n for _f, n, _c, _d in REWARD_ICON_SPECS if n not in have]
    if missing:
        print(f"  缺模板: {'、'.join(missing)}  (颜色判据仍能认出它们, 模板只是兜底)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
