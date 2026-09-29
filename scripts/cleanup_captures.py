#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""截图目录例行清理 —— 每日自动跑一次, 让 `captures/` 的体积**封顶**。

## 三层闸门 (2026-09-29 定案)

体积维持不能只靠一个参数, 因为参数会**失效**。所以分三层, 每层挡不同的东西:

    1. 源头减量 : 奖励图存 JPEG q88 (省 88%)           <- 挡"单张太大", 在 battler.py
    2. 按龄回收 : 异常图 30 天 / 奖励图 90 天          <- 挡"越攒越多", 本脚本 --days
    3. 硬上限   : captures/ 总量超过 --max-mb 就从最旧删 <- 挡"速率突变", 本脚本 --max-mb

第 3 层是**兜底**, 不是重复劳动。理由很实在: 2026-09-29 实测当天写了 57 张 / 86.8 MB
奖励图, 是先前估计的两倍 —— 同一个"保留 365 天"从"约 5 GB"直接变成 **30.9 GB**。
保留期是**按时间**设闸, 它管不了**速率**变化; 上限才是几何意义上的"不会爆"。

## 保留策略

    captures/rewards/**   开箱奖励图 —— 保留 --rewards-days 天 (默认 90)
    captures/ 其它        异常现场图 —— 超过 --days 天就删 (默认 30)
    outputs/              临时调试产物, 同样按 --days 清

奖励图为什么**不是永久**: 用户的口径是"图片每个月清一次, 我只需要看开箱的奖励
**是什么**就行" —— "是什么"这件事已经以**文字**落在 `data/rewards.jsonl` 里 (物品名 +
胜负 + 时间), 那份记录永不删、也几乎不占空间。图只是佐证, 留一个季度足够回看。

## 安全设计

* 目标目录**从 Config() 现算**, 且断言必须在仓库根之下 —— 绝不吃传进来的任意路径。
* 奖励图的保护是**按路径前缀**判断的 (`skip=("rewards",)`), 不是按文件名通配。
* 只删文件, 目录空了才顺手删掉; 任何删除失败都不中断, 只记数。
* 硬上限删除**不碰 `--max-min-age` 小时内的新文件** (默认 6h) —— 当天正在写的截图
  不是垃圾, 删掉"刚刚那次异常"的现场往往就是删掉了最想看的证据。
* `--dry` 先看清单, 这是默认推荐的第一步。

跑法:
    python scripts/cleanup_captures.py --dry                    # 预览 (30 天 / 90 天 / 无上限)
    python scripts/cleanup_captures.py --dry --days 7           # 换个保留期预览
    python scripts/cleanup_captures.py --rewards-days 180
    python scripts/cleanup_captures.py --rewards-days 0         # 奖励图也按 --days 清
    python scripts/cleanup_captures.py --max-mb 1500 --dry      # 只看硬上限会删什么
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

# PNG/JPEG 的单位体积比 —— 由 `scripts/reward_storage_selftest.py` E 段在 3 张真实奖励整帧上
# 实测 (1.51/1.49/1.55 MB -> 186/180/200 KB), 均值约 8.2x。**改 REWARD_IMG_QUALITY 后要重测**,
# 否则 --status 的"转 JPEG 后估算"会变成一个撒谎的数字。
REWARD_JPEG_RATIO = 8.2


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


def _usage(root: pathlib.Path) -> tuple[int, int]:
    """(总字节, 文件数)。目录不存在返回 (0, 0)。"""
    if not root.exists():
        return 0, 0
    b = c = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                b += p.stat().st_size
                c += 1
            except OSError:
                pass
    return b, c


def _enforce_cap(root: pathlib.Path, cap_bytes: int, dry: bool,
                 min_age_hours: float = 6.0) -> tuple[int, int, list[str]]:
    """**硬上限兜底**: 总量超过 cap_bytes 时, 从最旧的文件开始删, 直到降回上限以内。

    为什么保留期之外还要这一层 (2026-09-29)
        "保留 90 天"是**按时间**设闸, 它挡不住**速率**变化 —— 一旦落盘格式、截图频率或
        分辨率变了 (09-29 当天就实测到 86.8 MB/天, 是先前估计的两倍), 同一个保留期会
        突然从"1 GB"变成"30 GB"。保留期是**意图**, 上限才是**保证**。

    删除顺序 = **全局 mtime 升序** (最旧的先走), 不做目录区分: 到了要靠上限保命的地步,
    已经没有"哪类图更金贵"的余地了, 最旧的天然是信息价值最低的。

    年龄不足 `min_age_hours` 的文件**一律不动**: 当天正在写的截图不是垃圾, 而且删掉
    "刚刚那次异常"的现场往往是删掉了最想看的证据。默认 6 小时 —— 比任何一次会话都长,
    又比"一天"短, 保证上限在下一个任务周期内一定能收住。
    """
    total, _ = _usage(root)
    if total <= cap_bytes:
        return 0, 0, []
    cutoff = time.time() - min_age_hours * 3600
    cands: list[tuple[float, pathlib.Path, int]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:       # 太新, 不动
            continue
        cands.append((st.st_mtime, p, st.st_size))
    cands.sort(key=lambda x: x[0])      # 最旧优先

    n = freed = 0
    samples: list[str] = []
    for _, p, sz in cands:
        if total <= cap_bytes:
            break
        rel = p.relative_to(root).as_posix()
        n += 1
        freed += sz
        total -= sz
        if len(samples) < 8:
            samples.append(f"{rel}  ({_human(sz)})  超出上限, 最旧优先")
        if not dry:
            try:
                p.unlink()
            except OSError:
                n -= 1
                freed -= sz
                total += sz
    if not dry:
        for d in sorted(root.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
    return n, freed, samples


def _status(capture_dir: pathlib.Path, outputs_dir: pathlib.Path,
            max_mb: int) -> int:
    """只读体检: 钱花在哪儿了、涨多快、离上限还有多远。**不删任何东西。**

    加它的理由: 三层闸门如果不可见, 用户就只能靠"我觉得最近好像变大了"来判断有没有生效。
    体积这件事必须能一条命令问出来。
    """
    print("=== captures/ 体积体检 (只读, 未删任何文件) ===")
    if not capture_dir.exists():
        print(f"  {capture_dir} 不存在")
        return 0

    # ---- 分档统计 ----
    buckets: dict[str, list[int]] = {}
    for p in capture_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(capture_dir).as_posix()
        parts = rel.split("/")
        if parts[0] == "rewards":
            if len(parts) >= 3 and parts[1].startswith("2") and len(parts[1]) == 8:
                key = "rewards/<日期>/"               # 具体天在上面的"按天"里单独聚合
            else:
                key = "rewards/" + (parts[1] if len(parts) > 1 else "")
        else:
            key = "(顶层异常现场图)" if p.name.startswith("anomaly_") else "(顶层其它)"
        try:
            buckets.setdefault(key, []).append(p.stat().st_size)
        except OSError:
            pass

    order = sorted(buckets.items(), key=lambda kv: -sum(kv[1]))
    grand = 0
    for k, sizes in order:
        s = sum(sizes)
        grand += s
        print(f"  {_human(s):>10s}  {len(sizes):5d} 个   {k}")
    print(f"  {'-'*46}")
    print(f"  {_human(grand):>10s}          合计")

    # ---- 奖励图按天: 看增速 ----
    rw = capture_dir / "rewards"
    days: dict[str, list[int]] = {}
    n_jpg = n_png = 0
    if rw.exists():
        for p in rw.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in (".jpg", ".jpeg"):
                n_jpg += 1
            elif p.suffix.lower() == ".png":
                n_png += 1
            if p.parent.name.isdigit() and len(p.parent.name) == 8:
                try:
                    days.setdefault(p.parent.name, []).append(p.stat().st_size)
                except OSError:
                    pass
    if days:
        print("\n  奖励图按天 (最近 7 天):")
        for d in sorted(days)[-7:]:
            sizes = days[d]
            print(f"    {d}  {len(sizes):4d} 张  {_human(sum(sizes)):>10s}"
                  f"   均 {_human(sum(sizes)/len(sizes))}")
        per_day = sum(sum(v) for v in days.values()) / len(days)

        # 存量历史是 PNG, 新图是 JPEG —— 两者的单位成本差 8 倍多, 混在一起算会吓人也算不准
        print(f"    格式构成: JPEG {n_jpg} 张 / PNG {n_png} 张"
              + ("   (全是历史存量, 新图起才是 JPEG)" if n_jpg == 0 else ""))
        print(f"    日均 (按现有混合): {_human(per_day)}")
        # REWARD_JPEG_RATIO 是 reward_storage_selftest E 段在真实帧上实测的 PNG/JPEG 比
        jpg_day = per_day / REWARD_JPEG_RATIO if n_png and not n_jpg else per_day
        print(f"    转 JPEG 后估算日均: {_human(jpg_day)}")
        for span, label in ((30, "30 天"), (90, "90 天"), (365, "365 天")):
            print(f"       × {label:>7s} ≈ {_human(jpg_day*span)}")

    # ---- 异常图按天 ----
    an = [p for p in capture_dir.glob("anomaly_*.png")]
    if an:
        print(f"\n  异常现场图: {len(an)} 张 / {_human(sum(p.stat().st_size for p in an))}"
              f"   (均在顶层, 保留 30 天)")

    # ---- outputs ----
    if outputs_dir.exists():
        fs = [p for p in outputs_dir.rglob("*") if p.is_file()]
        print(f"  outputs/: {len(fs)} 个 / {_human(sum(p.stat().st_size for p in fs))}")

    # ---- 离上限多远 ----
    if max_mb > 0:
        cap = max_mb * 1024 * 1024
        pct = 100.0 * grand / cap if cap else 0
        bar_len = 34
        fill = min(bar_len, int(bar_len * grand / cap)) if cap else 0
        bar = "#" * fill + "." * (bar_len - fill)
        print(f"\n  硬上限: [{bar}] {pct:.1f}%   {_human(grand)} / {max_mb} MB")
        print(f"  {'⚠ 已超上限, 下次任务会从最旧开始删' if grand > cap else f'余量 {_human(cap - grand)}'}")
    else:
        print(f"\n  硬上限: 未启用 (--max-mb 0)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="清理运行期截图 (异常图 30 天 / 奖励图 90 天)")
    ap.add_argument("--days", type=int, default=30, help="异常现场图的保留天数, 默认 30")
    ap.add_argument("--rewards-days", type=int, default=90,
                    help="开箱奖励图的保留天数, 默认 90 (设 0 = 也按 --days 清)")
    ap.add_argument("--dry", action="store_true", help="只列清单, 不真删")
    ap.add_argument("--keep-outputs", action="store_true", help="不碰 outputs/")
    ap.add_argument("--no-dedup", action="store_true",
                    help="跳过'同一天同画面只留一张'这一步 (默认会做)")
    ap.add_argument("--max-mb", type=int, default=2000,
                    help="captures/ 总量硬上限(MB), 0=不启用; 超出则从最旧开始删到上限以内"
                         " (默认 2000, 与计划任务一致 —— 手动跑一遍看到的结果就该等于任务真会做的事)")
    ap.add_argument("--max-min-age", type=float, default=6.0,
                    help="硬上限删除时不碰的新文件年龄(小时), 默认 6")
    ap.add_argument("--status", action="store_true",
                    help="只打印体积体检 (分档占用/日均增速/离上限多远), 什么都不删")
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

    if args.status:
        return _status(capture_dir, outputs_dir, args.max_mb)

    mode = "预览 (未删除任何文件)" if args.dry else "执行"
    print(f"=== 截图清理 [{mode}] 异常图 {args.days} 天 / 奖励图 {args.rewards_days} 天"
          + (f" / 硬上限 {args.max_mb} MB" if args.max_mb > 0 else "") + " ===")
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

    # 5) 硬上限兜底 (最后一道闸: 保留期挡不住"速率变化", 上限才挡得住)
    if args.max_mb > 0:
        cap = args.max_mb * 1024 * 1024
        used, cnt = _usage(capture_dir)
        print(f"\n-- captures/ 硬上限          {_human(used)} / 上限 {args.max_mb} MB  "
              f"({cnt} 个文件)")
        if used <= cap:
            print(f"     未超上限, 不触发 (余量 {_human(cap - used)})")
        else:
            n4, b4, s4 = _enforce_cap(capture_dir, cap, args.dry,
                                      min_age_hours=args.max_min_age)
            print(f"     超 {_human(used - cap)}, 从最旧开始删 {n4} 个 / {_human(b4)}"
                  f"  (不动 {args.max_min_age:g} 小时内的新文件)")
            for s in s4:
                print(f"       {s}")
            if n4 == 0:
                print("     ⚠ 没有可删的旧文件 (全部都在保护期内) —— 体积会继续涨, "
                      "要么调低 --rewards-days, 要么调低落盘质量 REWARD_IMG_QUALITY")
            total_n += n4
            total_b += b4

    print(f"\n合计: {total_n} 个文件, {_human(total_b)}"
          + ("  (预览, 不带 --dry 才真删)" if args.dry else "  已释放"))
    if args.max_mb > 0:
        after, acnt = _usage(capture_dir)
        print(f"captures/ 清理后: {_human(after)} / 上限 {args.max_mb} MB  ({acnt} 个文件)"
              + ("  (预览值)" if args.dry else ""))
    print("提示: 奖励的**物品名**另存在 data/rewards.jsonl, 那份文字记录永不被清 —— "
          "所以图清了也查得到\"当时拿到了什么\"。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
