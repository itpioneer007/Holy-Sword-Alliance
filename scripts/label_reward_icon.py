#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开箱奖励图标 —— 「待命名」图标的补命名与历史回填。

## 为什么需要它

`reward_icons.match_roi` 认不出新图标时**不猜** —— 它返回「待命名」, 并把那枚图标存到
`captures/rewards/_unknown/`。这是刻意的: 猜错会污染用户的奖励统计。但"待命名"得有出口,
这个脚本就是出口。

## 三个动作

    --list                     看待命名队列 (图标文件 + 色相构成, 不用开图就能判断)
    --promote <源图> <slug>    把某枚图标提升为模板 (写进 assets/reward_icons/)
    --backfill                 用当前识别器重扫全部历史记录: 补 item 字段 + 统一文件名

补命名是**低频且由人判断**的动作, 所以 `--promote` 只负责搬文件, 不自动改代码 ——
物品名与颜色口径记在 `sj_bot/reward_icons.py` 的 `REWARD_ICON_SPECS` 里 (那是一目了然的
单一数据源), `--promote` 会把该粘的那一行直接打印出来。

跑法:
    python scripts/label_reward_icon.py --list
    python scripts/label_reward_icon.py --promote captures/rewards/_unknown/172000.png rune_exp_green
    python scripts/label_reward_icon.py --backfill
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.config import Config                        # noqa: E402
from sj_bot.reward_icons import (                       # noqa: E402
    ICON_DIR, UNKNOWN_NAME, apply_mask, crop_icon, hue_shares, match_roi,
)


def _imread(p: pathlib.Path):
    """中文路径必须走 fromfile, 否则 cv2.imread 静默返回 None。"""
    if not p.exists():
        return None
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def _imwrite(p: pathlib.Path, img) -> bool:
    """中文路径必须走 imencode().tofile(); 注意 tofile() 成功时返回 None, 不能拿返回值判成败。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", img)[1].tofile(str(p))
    return p.exists()


def _brief_shares(sh: dict) -> str:
    items = [(k, v) for k, v in sh.items() if v >= 5]
    items.sort(key=lambda kv: -kv[1])
    return " ".join(f"{k}{v:.0f}%" for k, v in items) or "无彩色"


def _load_jsonl(rp: pathlib.Path) -> list[dict]:
    if not rp.exists():
        return []
    out = []
    for line in rp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- --list

def cmd_list() -> int:
    cfg = Config()
    unk_dir = pathlib.Path(cfg.capture_dir) / "rewards" / "_unknown"
    files = sorted(unk_dir.glob("*.png")) if unk_dir.exists() else []
    print(f"待命名队列: {len(files)} 枚  ({unk_dir})")
    if files:
        print(f"  {'文件':22s} {'尺寸':10s} 色相构成")
        for p in files:
            img = _imread(p)
            if img is None:
                print(f"  {p.name:22s} 读不出来")
                continue
            print(f"  {p.name:22s} {img.shape[1]}x{img.shape[0]:<6d} {_brief_shares(hue_shares(img))}")

    rp = pathlib.Path(cfg.data_dir) / "rewards.jsonl"
    rows = _load_jsonl(rp)
    pend = [r for r in rows if (r.get("item") or UNKNOWN_NAME) == UNKNOWN_NAME]
    print(f"\n记录里 item=待命名 的条数: {len(pend)} / 总 {len(rows)}")
    if pend:
        print("  跑 --backfill 可以用当前识别器重扫一遍 (模板补充后尤其值得跑)")
    print("\n补命名流程:")
    print("  1) 打开上面某个 png 看清楚是什么")
    print("  2) python scripts/label_reward_icon.py --promote <那个png> <模板名>")
    print("  3) 把脚本打印的那行加进 sj_bot/reward_icons.py 的 REWARD_ICON_SPECS")
    print("  4) python scripts/label_reward_icon.py --backfill   (补上历史记录)")
    return 0


# ---------------------------------------------------------------- --promote

def cmd_promote(src: str, slug: str) -> int:
    p = pathlib.Path(src)
    if not p.is_absolute():
        p = ROOT / p
    img = _imread(p)
    if img is None:
        print(f"读不出图: {p}")
        return 1
    # 源图可能是 captures/rewards/_unknown/ 里已裁好的 ROI, 也可能是整帧 —— 都兼容
    if img.shape[0] > 300:
        img = crop_icon(img)
        if img is None:
            print("从整帧里裁不出图标 ROI (帧尺寸不对?)")
            return 1
    dst = ICON_DIR / f"{slug}.png"
    if not _imwrite(dst, apply_mask(img)):
        print(f"写入失败: {dst}")
        return 1
    print(f"已写入模板: {dst}  ({img.shape[1]}x{img.shape[0]})")
    print(f"色相构成: {_brief_shares(hue_shares(img))}")
    print("\n把下面这行加进 sj_bot/reward_icons.py 的 REWARD_ICON_SPECS (按需改物品名/颜色):")
    print(f'    ("{slug}.png", "物品名", "色", "图案说明"),')
    print("\n加完后跑 python scripts/label_reward_icon.py --backfill 回填历史记录")
    return 0


# ---------------------------------------------------------------- --backfill

def _retarget(cfg, rec: dict, item: str) -> str | None:
    """按 <HHMMSS>_<物品名>_<胜负>.png 重建文件名 (幂等)。返回新相对路径或 None。"""
    old = pathlib.Path(cfg.capture_dir) / rec["file"]
    if not old.exists():
        return None
    m = re.match(r"^(\d{6})", old.stem)
    base = m.group(1) if m else old.stem
    verdict = rec.get("verdict") or "unknown"
    want = f"{base}_{item}_{verdict}{old.suffix}"
    if old.name == want:
        return None
    newp = old.with_name(want)
    if newp.exists():                       # 同秒同名冲突时保留原文件, 不乱覆盖
        return None
    old.rename(newp)
    return str(newp.relative_to(cfg.capture_dir))


def cmd_backfill() -> int:
    cfg = Config()
    rp = pathlib.Path(cfg.data_dir) / "rewards.jsonl"
    rows = _load_jsonl(rp)
    if not rows:
        print("没有记录")
        return 0

    changed_item = renamed = 0
    for rec in rows:
        p = pathlib.Path(cfg.capture_dir) / (rec.get("file") or "")
        img = _imread(p)
        if img is None:
            print(f"  跳过 (图不在): {rec.get('file')}")
            continue
        item, conf, src = match_roi(crop_icon(img))
        before = rec.get("item")
        if before != item:
            rec["item"], rec["item_conf"], rec["item_src"] = item, round(conf, 3), src
            changed_item += 1
            print(f"  {rec['ts']}  {before or '(无)'} -> {item}  (置信 {conf:.2f}/{src})")
        new_rel = _retarget(cfg, rec, item)
        if new_rel:
            rec["file"] = new_rel
            renamed += 1

    tmp = rp.with_name(rp.name + ".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    tmp.replace(rp)                          # 原子替换
    print(f"\n回填完成: item 更新 {changed_item} 条, 文件改名 {renamed} 个, 共 {len(rows)} 条记录")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="开箱奖励图标: 待命名队列 / 补命名 / 历史回填")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="看待命名队列与统计")
    g.add_argument("--backfill", action="store_true", help="用当前识别器回填历史记录")
    g.add_argument("--promote", nargs=2, metavar=("<源图>", "<模板名>"),
                   help="把一枚图标提升为模板")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.backfill:
        return cmd_backfill()
    return cmd_promote(*args.promote)


if __name__ == "__main__":
    raise SystemExit(main())
