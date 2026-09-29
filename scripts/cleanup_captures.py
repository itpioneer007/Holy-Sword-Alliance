#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""截图目录例行清理 —— 每月自动跑一次, 只留开箱奖励图。

## 为什么需要

`captures/` 是运行期产物: 每局异常会落一张 2.7MB 的全屏图, 一天十几张就是几十 MB,
一年下来几百 MB ~ 数 GB。分两档保留:

    captures/rewards/**   开箱奖励图 —— 保留 --rewards-days 天 (默认 365)
    captures/ 其它        异常现场图 —— 超过 --days 天就删 (默认 30)
    outputs/              临时调试产物, 同样按 --days 清

奖励图为什么也**不是永久**: 用户的口径是"图片每个月清一次, 我只需要看开箱的奖励
**是什么**就行" —— "是什么"这件事已经以**文字**落在 `data/rewards.jsonl` 里 (物品名 +
胜负 + 时间), 那份记录永不删、也几乎不占空间。图只是佐证, 留一年足够回看, 不必永久。

## 安全设计

* 目标目录**从 Config() 现算**, 且断言必须在仓库根之下 —— 绝不吃传进来的任意路径。
* 奖励图的保护是**按路径前缀**判断的 (`skip=("rewards",)`), 不是按文件名通配。
* 只删文件, 目录空了才顺手删掉; 任何删除失败都不中断, 只记数。
* `--dry` 先看清单, 这是默认推荐的第一步。

跑法:
    python scripts/cleanup_captures.py --dry                    # 预览 (异常图 30 天 / 奖励图 365 天)
    python scripts/cleanup_captures.py --dry --days 7           # 换个保留期预览
    python scripts/cleanup_captures.py --days 30 --rewards-days 180
    python scripts/cleanup_captures.py --days 30 --rewards-days 0   # 奖励图也按 30 天清
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.config import Config                        # noqa: E402


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sweep(root: pathlib.Path, days: int, dry: bool, skip: tuple[str, ...] = (),
           only: str | None = None) -> tuple[int, int, list[str]]:
    """清 root 下 mtime 早于 days 天的文件。返回 (删除数, 释放字节, 示例清单)。

    `skip`: 这些前缀下的文件一个都不碰。
    `only`: 只处理这个前缀下的文件 (与 skip 互斥使用, 用于给奖励图单独设保留期)。
    """
    if not root.exists():
        return 0, 0, []
    cutoff = time.time() - days * 86400
    n = 0
    freed = 0
    samples: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if only is not None:
            if not (rel == only or rel.startswith(only + "/")):
                continue
        elif any(rel == k or rel.startswith(k + "/") for k in skip):
            continue                                    # 受保护子树, 一个都不碰
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue
        n += 1
        freed += st.st_size
        if len(samples) < 8:
            samples.append(f"{rel}  ({_human(st.st_size)})")
        if not dry:
            try:
                p.unlink()
            except OSError:
                n -= 1
                freed -= st.st_size
    # 删除后顺手收掉空目录 (不加 --dry)
    if not dry:
        for d in sorted(root.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
    return n, freed, samples


def main() -> int:
    ap = argparse.ArgumentParser(description="清理运行期截图 (异常图 30 天 / 奖励图 365 天)")
    ap.add_argument("--days", type=int, default=30, help="异常现场图的保留天数, 默认 30")
    ap.add_argument("--rewards-days", type=int, default=365,
                    help="开箱奖励图的保留天数, 默认 365 (设 0 = 也按 --days 清)")
    ap.add_argument("--dry", action="store_true", help="只列清单, 不真删")
    ap.add_argument("--keep-outputs", action="store_true", help="不碰 outputs/")
    args = ap.parse_args()

    if args.days < 1:
        print("--days 至少为 1 (想全清就把天数设成 1)")
        return 2
    if args.rewards_days < 0:
        print("--rewards-days 不能为负")
        return 2

    cfg = Config()
    capture_dir = pathlib.Path(cfg.capture_dir).resolve()
    outputs_dir = (ROOT / "outputs").resolve()

    # 安全闸: 目标必须在仓库根之下。Config 若被改歪, 这里立刻拦住而不是删错地方。
    for d in (capture_dir, outputs_dir):
        try:
            d.relative_to(ROOT)
        except ValueError:
            print(f"拒绝执行: {d} 不在仓库根 {ROOT} 之下")
            return 2

    mode = "预览 (未删除任何文件)" if args.dry else "执行"
    print(f"=== 截图清理 [{mode}] 异常图 {args.days} 天 / 奖励图 {args.rewards_days} 天 ===")
    print(f"  captures: {capture_dir}")

    total_n = total_b = 0

    # 1) 异常现场图等: 排除 rewards/ 子树
    n, b, samples = _sweep(capture_dir, args.days, args.dry, skip=("rewards",))
    print(f"\n-- captures/ (不含 rewards/)  匹配 {n} 个, {_human(b)}")
    for s in samples:
        print(f"     {s}")
    total_n += n
    total_b += b

    # 2) 奖励图: 单独一档保留期
    if args.rewards_days >= 1:
        n2, b2, s2 = _sweep(capture_dir, args.rewards_days, args.dry, only="rewards")
        print(f"-- captures/rewards/          匹配 {n2} 个, {_human(b2)}  (保留 {args.rewards_days} 天)")
        for s in s2:
            print(f"     {s}")
        total_n += n2
        total_b += b2

    # 3) outputs/ 临时产物
    if not args.keep_outputs:
        n3, b3, s3 = _sweep(outputs_dir, args.days, args.dry)
        print(f"-- outputs/                   匹配 {n3} 个, {_human(b3)}")
        for s in s3:
            print(f"     {s}")
        total_n += n3
        total_b += b3

    print(f"\n合计: {total_n} 个文件, {_human(total_b)}"
          + ("  (预览, 不带 --dry 才真删)" if args.dry else "  已释放"))
    print("提示: 奖励的**物品名**另存在 data/rewards.jsonl, 那份文字记录永不被清 —— "
          "所以图清了也查得到\"当时拿到了什么\"。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
