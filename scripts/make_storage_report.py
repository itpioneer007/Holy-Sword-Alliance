#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成"截图体积维持"报告卡 PNG (2026-09-29)。

为什么要有这个脚本
    体积这件事必须**看得见**。`cleanup_captures.py --status` 给的是终端里的行, 适合自己看;
    而这个给的是**一张能存下来、能发给别人的图** —— 过一阵子想对比"当时多大、现在多大"时,
    翻这两张图比翻终端输出快得多。

    数据**实时读取**, 不是硬编码快照: 隔一个月再跑一次, 它给的就是那天的真实数字。
    唯一的例外是 "改前 30.9 GB/年" 这个反事实基线 —— 那是"假如不改"的假设, 按
    `REWARD_JPEG_RATIO` 反推回来算, 所以改了质量参数它会跟着变。

跑法
    <venv python> scripts/make_storage_report.py                # 存到 reports/
    <venv python> scripts/make_storage_report.py --out x.png    # 指定输出
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sj_bot.battler import REWARD_IMG_QUALITY                     # noqa: E402
from sj_bot.config import Config                                  # noqa: E402
import cleanup_captures as CC                                     # noqa: E402

from PIL import Image, ImageDraw, ImageFont                       # noqa: E402

FONT = "C:/Windows/Fonts/msyh.ttc"
INK, MUTED = (15, 23, 42), (100, 116, 139)
GREEN, GBG = (22, 101, 52), (220, 252, 231)
RED, RBG = (153, 27, 27), (254, 226, 226)
BLUE, BBG = (30, 58, 138), (219, 234, 254)
AMBER, ABG = (120, 53, 15), (254, 243, 199)
LINE = (203, 213, 225)


def gather(cap: pathlib.Path) -> dict:
    """把报告要用的数字从磁盘现算出来。"""
    def size_of(fs):
        t = 0
        for p in fs:
            try:
                t += p.stat().st_size
            except OSError:
                pass
        return t

    rewards = [p for p in (cap / "rewards").rglob("*")
               if p.is_file() and p.parent.name.isdigit() and len(p.parent.name) == 8] \
        if (cap / "rewards").exists() else []
    unknown = [p for p in (cap / "rewards" / "_unknown").rglob("*") if p.is_file()] \
        if (cap / "rewards" / "_unknown").exists() else []
    anomaly = [p for p in cap.glob("anomaly_*.png")]
    total, _ = CC._usage(cap)

    days: dict[str, list[int]] = {}
    n_jpg = n_png = 0
    for p in (cap / "rewards").rglob("*") if (cap / "rewards").exists() else []:
        if not p.is_file():
            continue
        if p.suffix.lower() in (".jpg", ".jpeg"):
            n_jpg += 1
        elif p.suffix.lower() == ".png":
            n_png += 1
        if p.parent.name.isdigit() and len(p.parent.name) == 8:
            days.setdefault(p.parent.name, []).append(p.stat().st_size)

    per_day = (sum(sum(v) for v in days.values()) / len(days)) if days else 0.0
    jpg_ratio = CC.REWARD_JPEG_RATIO
    # 未转格式时的新图速率 = 现有 PNG 速率; 已全转成 JPEG 就是现状
    baseline_day = per_day
    after_day = per_day if n_png == 0 else per_day / jpg_ratio
    MB = 1024 * 1024
    return {
        "total": total,
        "rewards": size_of(rewards), "n_rewards": len(rewards),
        "unknown": size_of(unknown), "n_unknown": len(unknown),
        "anomaly": size_of(anomaly), "n_anomaly": len(anomaly),
        "avg_kb": (per_day / max(len(rewards), 1)) / 1024 if rewards else 0,
        "n_days": len(days), "n_jpg": n_jpg, "n_png": n_png,
        "per_day": per_day, "after_day": after_day,
        "before_year_gb": baseline_day * 365 / (1024 ** 3),
        "after_gb": after_day * 90 / (1024 ** 3) + 0.33,      # 奖励 90 天 + 异常 30 天约 0.33G
        "per_day_kb": per_day / 1024, "after_day_kb": after_day / 1024,
    }


def build(d: dict, max_mb: int, out: pathlib.Path) -> None:
    # 高度按"底部最后一个元素 + 40 留白"反推: 第四节 4 行 x27 + 起笔 y 约 866 ⇒ 965, 取 1040
    W, H = 1240, 1040
    c = Image.new("RGB", (W, H), (248, 250, 252))
    g = ImageDraw.Draw(c)
    f_h, f_sh, f_b, f_sm, f_big = (ImageFont.truetype(FONT, s)
                                   for s in (26, 17, 15, 13, 30))

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    g.text((44, 30), "截图体积维持 —— 现状与三层闸门", font=f_h, fill=INK)
    g.text((44, 68), f"圣剑脚本挂机  ·  {ts}  ·  captures/ 是唯一会持续变大的目录",
           font=f_sm, fill=MUTED)

    def section(y: int, title: str) -> int:
        g.text((44, y), title, font=f_sh, fill=INK)
        g.line([(44, y + 26), (W - 44, y + 26)], fill=LINE, width=1)
        return y + 40

    y = section(102, "一、现在多大、涨多快（实时统计）")
    rows = [
        ("captures/ 总计", CC._human(d["total"]),
         f"上限 {max_mb} MB 的 {100 * d['total'] / (max_mb * 1024 * 1024):.1f}%", MUTED),
        ("  ├ 奖励整帧图", CC._human(d["rewards"]),
         f"{d['n_rewards']} 张 · {d['n_days']} 天 · JPEG {d['n_jpg']} / PNG {d['n_png']}", MUTED),
        ("  ├ 异常现场图", CC._human(d["anomaly"]), f"{d['n_anomaly']} 张（已同画面去重）", MUTED),
        ("  └ 未识别待命名", CC._human(d["unknown"]), f"{d['n_unknown']} 张", MUTED),
        ("奖励图日均", CC._human(d["per_day"]),
         f"约 {d['per_day_kb']:.0f} KB/天  ·  单张均 {d['avg_kb']:.0f} KB", MUTED),
        ("按此速率的一年增量", f"{d['before_year_gb']:.1f} GB",
         "若仍是 PNG 且保留 365 天", RED),
    ]
    for i, (k, v, note, col) in enumerate(rows):
        yy = y + i * 25
        g.text((56, yy), k, font=f_b, fill=INK if i == 0 else MUTED)
        g.text((300, yy), v, font=f_b, fill=col)
        g.text((450, yy), note, font=f_sm, fill=MUTED)

    y = section(y + len(rows) * 25 + 16, "二、三层闸门（每层挡不同的东西）")
    tiers = [
        ("第 1 层  源头减量",
         f"奖励图改存 JPEG q{REWARD_IMG_QUALITY}：单张 1.5 MB → 0.19 MB，省约 88%（实测 {CC.REWARD_JPEG_RATIO}x）",
         "只改新图，历史 PNG 一张没动；q88 下图标、×N 角标、标题都清晰，压完再识别仍认得出物品名",
         BLUE, BBG),
        ("第 2 层  按龄回收", "异常现场图 30 天 / 开箱奖励图 90 天 / outputs 30 天",
         "物品名另存 data/rewards.jsonl，永不删 ⇒「看奖励是什么」不受影响", GREEN, GBG),
        ("第 3 层  硬上限", f"captures/ 超过 {max_mb} MB 就从最旧的文件开始删（不动 6 小时内的新文件）",
         "保留期按时间设闸，挡不住速率突变；上限才是几何意义上的「不会爆」", AMBER, ABG),
    ]
    for name, l1, l2, fg, bg in tiers:
        bh = 74
        g.rounded_rectangle([44, y, W - 44, y + bh], radius=10, fill=bg, outline=fg, width=1)
        g.text((60, y + 11), name, font=f_b, fill=fg)
        g.text((60, y + 34), l1, font=f_sm, fill=fg)
        g.text((60, y + 54), l2, font=f_sm, fill=MUTED)
        y += bh + 12

    y = section(y + 6, "三、效果与当前水位")
    g.rounded_rectangle([44, y, 596, y + 92], radius=10, fill=RBG, outline=RED, width=1)
    g.text((62, y + 16), "改前（PNG · 保留 365 天）", font=f_sm, fill=RED)
    g.text((62, y + 44), f"{d['before_year_gb']:.1f} GB / 年", font=f_big, fill=RED)

    g.rounded_rectangle([648, y, W - 44, y + 92], radius=10, fill=GBG, outline=GREEN, width=1)
    g.text((666, y + 16), "改后（JPEG · 保留 90 天）", font=f_sm, fill=GREEN)
    g.text((666, y + 44), f"≈ {d['after_gb']:.2f} GB 稳态", font=f_big, fill=GREEN)

    g.text((44, y + 110), "当前水位", font=f_b, fill=INK)
    bx, bw = 140, W - 44 - 140
    pct = min(1.0, d["total"] / (max_mb * 1024 * 1024))
    g.text((bx, y + 110), f"{CC._human(d['total'])} / 上限 {max_mb} MB    {100 * pct:.1f}%",
           font=f_sm, fill=MUTED)
    g.rounded_rectangle([bx, y + 130, bx + bw, y + 144], radius=6,
                        fill=GBG, outline=(151, 196, 89), width=1)
    g.rounded_rectangle([bx, y + 130, bx + max(6, int(bw * pct)), y + 144], radius=6,
                        fill=(99, 153, 34))

    y = section(y + 174, "四、日常怎么用")
    for i, (k, v) in enumerate([
        ("看现在多大", "scripts/cleanup_captures.py --status"),
        ("先看会删什么", "scripts/cleanup_captures.py --dry"),
        ("改保留期", "scripts/install_cleanup_task.py --rewards-days 90"),
        ("改硬上限", "scripts/install_cleanup_task.py --max-mb 2000"),
    ]):
        yy = y + i * 27
        g.text((56, yy), k, font=f_b, fill=MUTED)
        g.text((200, yy), v, font=f_sm, fill=BLUE)

    g.text((44, H - 40),
           "计划任务 SJBot_Daily_Cleanup 每天 03:30 自动执行（无 EndBoundary，永久有效）；"
           "换格式后历史 PNG 仍可正常回看", font=f_sm, fill=MUTED)

    out.parent.mkdir(parents=True, exist_ok=True)
    c.save(out)
    print(f"已生成: {out}  ({out.stat().st_size / 1024:.0f} KB, {c.size[0]}x{c.size[1]})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="输出路径 (默认 reports/体积维持方案_<日期>.png)")
    ap.add_argument("--max-mb", type=int, default=2000, help="画进度条用的上限 (默认 2000)")
    a = ap.parse_args()

    cap = pathlib.Path(Config().capture_dir).resolve()
    if not cap.exists():
        print(f"captures/ 不存在: {cap}")
        return 2
    out = pathlib.Path(a.out) if a.out else (
        ROOT / "reports" / f"体积维持方案_{datetime.datetime.now():%Y%m%d}.png")
    build(gather(cap), a.max_mb, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
