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
    python scripts/cleanup_captures.py --dry --no-dedup         # 只看按龄删除, 不动重复帧

## 同画面去重 (2026-09-29)

默认**先做一遍去重**再按龄删: `captures/` 顶层的 `anomaly_*.png` 里, **同一天 + 同一
画面**只留最早一张。起因是 `Battler._save_anomaly` 原来每一轮重试都落一张图 ——
实测 39 张 / 71.7MB 里 66% (41MB) 是同屏幕近重复 (一次"导航 3 连败"就是 3 张同一画面,
主城被遮挡反复自愈能把同一屏留十几次; 09-29 18:20~18:23 那一次连着留了 11 张)。

* 判据 = `sj_bot.vision.same_screen` (32x18 灰度指纹, 见那里的实测标定, 阈值 2.5);
* **按天分批**: 同一画面出现在不同天说明"这毛病今天又犯了", 那是有效信息, 必须留;
* **只看画面, 不看 tag**: `main_city_rescue1` 与 `main_city_loop` 若是同一屏也合并 ——
  反正每次落盘的 **tag + 时刻都逐条写在 `data/logs/bot-*.log`** 里
  (`异常现场已存: anomaly_<HHMMSS>_<tag>.png`), "发生过几次 / 什么故障"不会丢;
* 丢的只是重复位图 —— 位图本身一个像素的信息都不少 (留下的那张就是同一画面)。
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


def _dedup_anomaly(capture_dir: pathlib.Path, dry: bool) -> tuple[int, int, list[str]]:
    """合并 captures/ 顶层 anomaly_*.png 里"同一天 + 同一画面"的重复帧。

    每组只留**最早**那张 (文件名是 HHMMSS, 但跨天会交错, 所以按 mtime 排序才算得对)。
    返回 (删除数, 释放字节, 示例清单)。
    """
    files = sorted((p for p in capture_dir.glob("anomaly_*.png") if p.is_file()),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        return 0, 0, []
    from sj_bot.vision import anomaly_fingerprint, imread_any, same_screen   # 惰性: 只有这步要 cv2

    kept: dict[str, list] = {}          # 天 -> [指纹]
    n = freed = 0
    samples: list[str] = []
    for p in files:
        try:
            day = time.strftime("%Y%m%d", time.localtime(p.stat().st_mtime))
            fp = anomaly_fingerprint(imread_any(p))
        except Exception:               # 读不开/解不了的帧一律保留, 绝不因为读失败而删
            continue
        bucket = kept.setdefault(day, [])
        if fp is not None and any(same_screen(f, fp) for f in bucket):
            n += 1
            sz = p.stat().st_size
            freed += sz
            if len(samples) < 8:
                samples.append(f"{p.name}  ({_human(sz)})  同日同画面")
            if not dry:
                try:
                    p.unlink()
                except OSError:         # 正在被写/占用 -> 当作没删, 计数回滚
                    n -= 1
                    freed -= sz
            continue
        if fp is not None:
            bucket.append(fp)
    return n, freed, samples


def main() -> int:
    ap = argparse.ArgumentParser(description="清理运行期截图 (异常图 30 天 / 奖励图 365 天)")
    ap.add_argument("--days", type=int, default=30, help="异常现场图的保留天数, 默认 30")
    ap.add_argument("--rewards-days", type=int, default=365,
                    help="开箱奖励图的保留天数, 默认 365 (设 0 = 也按 --days 清)")
    ap.add_argument("--dry", action="store_true", help="只列清单, 不真删")
    ap.add_argument("--keep-outputs", action="store_true", help="不碰 outputs/")
    ap.add_argument("--no-dedup", action="store_true",
                    help="跳过'同一天同画面只留一张'这一步 (默认会做)")
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

    # 1) 同一天 + 同一画面只留一张 (先做: 它是"去重", 不是"按龄淘汰")
    if not args.no_dedup:
        n0, b0, s0 = _dedup_anomaly(capture_dir, args.dry)
        print(f"\n-- captures/ 同画面去重      匹配 {n0} 个, {_human(b0)}")
        for s in s0:
            print(f"     {s}")
        total_n += n0
        total_b += b0

    # 2) 异常现场图等: 排除 rewards/ 子树
    n, b, samples = _sweep(capture_dir, args.days, args.dry, skip=("rewards",))
    print(f"\n-- captures/ (不含 rewards/)  匹配 {n} 个, {_human(b)}")
    for s in samples:
        print(f"     {s}")
    total_n += n
    total_b += b

    # 3) 奖励图: 单独一档保留期
    if args.rewards_days >= 1:
        n2, b2, s2 = _sweep(capture_dir, args.rewards_days, args.dry, only="rewards")
        print(f"-- captures/rewards/          匹配 {n2} 个, {_human(b2)}  (保留 {args.rewards_days} 天)")
        for s in s2:
            print(f"     {s}")
        total_n += n2
        total_b += b2

    # 4) outputs/ 临时产物
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
