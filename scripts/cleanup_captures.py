#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""截图目录例行清理 —— 每月自动跑一次, 只留开箱奖励图。

## 为什么需要

`captures/` 是运行期产物: 每局异常会落一张 2.7MB 的全屏图, 一天十几张就是几十 MB,
一年下来几百 MB ~ 数 GB。而用户实际只有一类图需要长期留: **开箱奖励** (那是"我到底
拿到了什么"的凭据)。所以:

    captures/rewards/**   永久保留 (除非显式 --no-keep-rewards)
    captures/ 其它        超过 --days 天就删
    outputs/              临时调试产物, 同样按天清 (里面是诊断脚本的中间图)

## 安全设计

* 目标目录**从 Config() 现算**, 且断言必须在仓库根之下 —— 绝不吃传进来的任意路径。
* `captures/rewards/` 的保留是**按路径前缀**判断的, 不是按文件名通配。
* 只删文件, 目录空了才顺手删掉; 任何删除失败都不中断, 只记数。
* `--dry` 先看清单, 这是默认推荐的第一步。

跑法:
    python scripts/cleanup_captures.py --dry              # 预览 (默认保留 30 天)
    python scripts/cleanup_captures.py --dry --days 7     # 换个保留期预览
    python scripts/cleanup_captures.py --days 30          # 真删
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.config import Config                        # noqa: E402

# 这些子路径**永久保留** —— 它们是用户要看的东西, 不是运行期垃圾
KEEP_PREFIXES = ("rewards",)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sweep(root: pathlib.Path, days: int, dry: bool,
           protect: tuple[str, ...] = ()) -> tuple[int, int, list[str]]:
    """清 root 下 mtime 早于 days 天的文件。返回 (删除数, 释放字节, 示例清单)。"""
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
        if any(rel == k or rel.startswith(k + "/") for k in protect):
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
    ap = argparse.ArgumentParser(description="清理运行期截图 (默认保留开箱奖励图)")
    ap.add_argument("--days", type=int, default=30, help="保留天数, 默认 30")
    ap.add_argument("--dry", action="store_true", help="只列清单, 不真删")
    ap.add_argument("--no-keep-rewards", action="store_true",
                    help="连 captures/rewards/ 也按天清 (默认永久保留)")
    ap.add_argument("--keep-outputs", action="store_true", help="不碰 outputs/")
    args = ap.parse_args()

    if args.days < 1:
        print("--days 至少为 1 (想全清就把天数设成 1)")
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

    protect = () if args.no_keep_rewards else KEEP_PREFIXES
    mode = "预览 (未删除任何文件)" if args.dry else "执行"
    print(f"=== 截图清理 [{mode}] 保留 {args.days} 天 ===")
    print(f"  captures: {capture_dir}")
    print(f"  保护子目录: {('、'.join(protect) + '/') if protect else '(无 — 全都按天清)'}")

    total_n = total_b = 0
    n, b, samples = _sweep(capture_dir, args.days, args.dry, protect)
    print(f"\n-- captures/  匹配 {n} 个, {_human(b)}")
    for s in samples:
        print(f"     {s}")
    total_n += n
    total_b += b

    if not args.keep_outputs:
        n2, b2, s2 = _sweep(outputs_dir, args.days, args.dry)
        print(f"-- outputs/   匹配 {n2} 个, {_human(b2)}")
        for s in s2:
            print(f"     {s}")
        total_n += n2
        total_b += b2

    print(f"\n合计: {total_n} 个文件, {_human(total_b)}"
          + ("  (预览, 加 --days N 不带 --dry 才真删)" if args.dry else "  已释放"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
