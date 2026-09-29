# -*- coding: utf-8 -*-
"""异常现场图「同画面去重」自测 (2026-09-29)。

背景 (真机实测)
    `Battler._save_anomaly` 原来每走到一次就把当前屏留下一张, 于是:
      · 一次"导航 3 连败" = 3 张画面几乎相同的图 (09-29 18:20~18:23 一连留了 11 张);
      · 主城被全屏模态盖住反复自愈时, 同一屏能留十几次。
    实测 `captures/` 顶层 39 张 / 71.7MB 里, **66% (41MB) 是同屏幕近重复**。

修法 (两处, 判据同源 = `sj_bot.vision.same_screen`)
    运行时 `Battler._save_anomaly`  与该进程内"已存过的指纹"比, 够像就跳过 (只记日志);
    事后   `cleanup_captures.py`     默认先做一遍"同一天同画面只留一张"再按龄删。

阈值 ANOMALY_DEDUP_DIFF = 2.5 的实测标定 (18 张真实帧两两全比 + 合成扰动)
    真实重复帧 (同屏重截 / 同天不同时刻 / 跨天同屏) ...... 0.1 ~ 1.5
    加 σ=16 高斯噪声 .................................... 0.58
    首张"确实不同"的帧对 (两张内容不同的黑屏) ............ 6.01
    不同页面 (主城 vs 幸运大转盘) ....................... > 20
  2.5 落在 1.5 与 6.01 之间 —— **刻意偏低**: 漏合并只多留一张图(无害),
  错合并会删掉真证据(不可逆)。

本自测锁死的契约
  A 段 判据本身 (合成图, 无外部依赖)
    A1 指纹形状/类型固定 = (18, 32) float32
    A2 空帧 -> 指纹 None; 且与任何指纹比都判**不同** (保守, 宁可多留图)
    A3 同图自比 = 同画面, 差 ≈ 0
    A4 真实重复的量级 (亮度 ±1 / σ=8 噪声) -> 仍判同画面
    A5 大的整体亮度偏移 (+12, 差 ≈11.7) -> 判**不同** —— 刻意的保守面:
       重截的同一屏不会整体变亮, 容忍它只会换来误并风险
    A6 结构完全不同 (上下分屏 vs 暗底亮横带) -> 判不同
    A7 阈值边界: 差 1.5 判同, 差 3.5 判不同 (阈值本身不准漂)
  B 段 批量去重 (`cleanup_captures._dedup_anomaly`, 临时目录, 不碰真实 captures/)
    B1 同一天 3 张同画面 -> 只留最早 1 张
    B2 同一画面出现在两天 -> **每天各留 1 张** (跨天保留 = "这毛病今天又犯了"的证据)
    B3 同一天的不同画面 -> 一张都不删
    B4 非 `anomaly_*.png` (如 reward_like.png) 一个都不碰
    B5 `dry=True` 只报数不删
    B6 坏图 (解不开) 一律保留 —— 绝不因读失败而删
    B7 幂等: 去重过再跑一次 = 0 删除
  C 段 运行时闸门 (`Battler._save_anomaly`, 桩化 _shot_img, 无 adb)
    C1 首帧 -> 落盘 + 日志"异常现场已存"
    C2 紧接着同画面 -> **不落盘**, 日志改记"不重复留图", 且是 info 级(不刷红)
    C3 换一个画面 -> 落盘
    C4 再回到第 1 个画面 -> 仍跳过 (比的是"已存过的集合", 不只比上一张)
    C5 指纹造不出来 (灰度 2 通道畸形帧) -> **保守落盘** (不确定就留)
  D 段 真实帧锚定 (assets/modal_ref/*.jpg, 不依赖 OCR 引擎)
    转盘页 / 背包页 / 干净主城 三者互判**不同画面** —— 证明阈值不会把不同页面误并成
    一张, 否则去重会把真证据删掉。样本放 assets/ 是因为 captures/ 会被月度清理。
"""
import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import cv2          # noqa: E402
import numpy as np  # noqa: E402

from sj_bot.battler import Battler                              # noqa: E402
from sj_bot.vision import (                                     # noqa: E402
    ANOMALY_DEDUP_DIFF, ANOMALY_FP_SIZE, anomaly_fingerprint, same_screen,
)

# 用显式路径加载清理脚本, 避免把 scripts/ 塞进 sys.path 造成模块名遮蔽
_spec = importlib.util.spec_from_file_location(
    "cleanup_captures_under_test", os.path.join(_ROOT, "scripts", "cleanup_captures.py"))
_cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cc)
_dedup_anomaly = _cc._dedup_anomaly

REF = Path(_ROOT) / "assets" / "modal_ref"

_fails: list[str] = []
_n = 0


def check(name, got, want):
    global _n
    _n += 1
    ok = got == want
    if not ok:
        _fails.append(f"{name}  期望={want!r} 实际={got!r}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return ok


# ---------------------------------------------------------------- 合成图工具
def frame(kind: str) -> np.ndarray:
    """900x1600 BGR 合成帧。A=上下分屏, B=暗底亮横带, flat=纯色。"""
    img = np.zeros((900, 1600, 3), np.uint8)
    if kind == "A":
        img[:450] = (200, 140, 60)
        img[450:] = (40, 90, 180)
    elif kind == "B":
        img[:] = (20, 25, 30)
        img[400:500, :] = (235, 240, 245)
    else:
        img[:] = (30, 30, 30)
    return img


def diff(a, b) -> float:
    return float(np.abs(a - b).mean())


def write(path: Path, img: np.ndarray) -> None:
    cv2.imencode(".png", img)[1].tofile(str(path))


# ============================================================ A 段
def sec_a():
    print("A 段 — 判据本身")
    a = frame("A")
    fp = anomaly_fingerprint(a)
    check("A1 指纹形状/类型 = (18,32) float32",
          (fp.shape, str(fp.dtype)), ((18, 32), "float32"))
    check("A1b 分辨率常量 = (32,18)", ANOMALY_FP_SIZE, (32, 18))
    check("A2 空帧 -> 指纹 None", anomaly_fingerprint(None), None)
    check("A2b None 与真指纹判不同", same_screen(None, fp), False)
    check("A2c 真指纹与 None 判不同", same_screen(fp, None), False)
    check("A3 自比 -> 同画面 (差 <1e-6)", (same_screen(fp, anomaly_fingerprint(a)),
                                          diff(fp, anomaly_fingerprint(a)) < 1e-6), (True, True))

    check("A4 亮度 +1 (真实重复的量级) -> 同画面",
          same_screen(fp, anomaly_fingerprint(
              np.clip(a.astype(np.int16) + 1, 0, 255).astype(np.uint8))), True)
    rng = np.random.default_rng(7)
    noisy = np.clip(a.astype(np.float32) + rng.normal(0, 8.0, a.shape), 0, 255).astype(np.uint8)
    check("A4b σ=8 噪声 -> 同画面 (缩放把噪声平均掉了)",
          same_screen(fp, anomaly_fingerprint(noisy)), True)

    bright12 = np.clip(a.astype(np.int16) + 12, 0, 255).astype(np.uint8)
    check(f"A5 亮度 +12 (差 {diff(fp, anomaly_fingerprint(bright12)):.1f}) -> 判不同 "
          f"(刻意保守面)", same_screen(fp, anomaly_fingerprint(bright12)), False)

    check(f"A6 结构完全不同 (差 {diff(fp, anomaly_fingerprint(frame('B'))):.1f}) -> 判不同",
          same_screen(fp, anomaly_fingerprint(frame("B"))), False)
    check("A6b 纯色底 -> 判不同", same_screen(fp, anomaly_fingerprint(frame("flat"))), False)

    zero = np.zeros(ANOMALY_FP_SIZE[::-1], np.float32)          # 形状 (18,32)
    check(f"A7 差 1.5 判同 (阈值 {ANOMALY_DEDUP_DIFF})",
          same_screen(zero, zero + (ANOMALY_DEDUP_DIFF - 1.0)), True)
    check("A7b 差 3.5 判不同", same_screen(zero, zero + (ANOMALY_DEDUP_DIFF + 1.0)), False)


# ============================================================ B 段
DAY1 = 1700000000.0          # 2023-11-15 06:13:20 (+08) —— 取在一天中段, 避免跨零点
DAY3 = DAY1 + 2 * 86400.0


def sec_b():
    print("B 段 — 批量去重 _dedup_anomaly")
    tmp = Path(tempfile.mkdtemp(prefix="sjdedup_"))
    try:
        a, b = frame("A"), frame("B")
        plan = [
            ("anomaly_101010_t1.png", a, DAY1 + 10),      # 当天第一个 -> 留
            ("anomaly_101020_t2.png", a, DAY1 + 20),      # 重复 -> 删
            ("anomaly_101030_t3.png", a, DAY1 + 30),      # 重复 -> 删
            ("anomaly_101100_other.png", b, DAY1 + 40),   # 不同画面 -> 留
            ("reward_like.png", a, DAY1 + 50),            # 非 anomaly_* -> 不碰
            ("anomaly_120000_t4.png", a, DAY3 + 10),      # 另一天的代表 -> 留
            ("anomaly_120100_t5.png", a, DAY3 + 20),      # 该天重复 -> 删
        ]
        for nm, img, mt in plan:
            write(tmp / nm, img)
            os.utime(tmp / nm, (mt, mt))
        # 坏图: 后缀是 png, 内容是垃圾
        (tmp / "anomaly_101200_broken.png").write_bytes(b"\x89PNG\r\n\x1a\n not-a-real-png")
        bad_mt = DAY1 + 60
        os.utime(tmp / "anomaly_101200_broken.png", (bad_mt, bad_mt))
        (tmp / "anomaly_notes.txt").write_text("x", encoding="utf-8")     # 非 png

        n_dry, b_dry, _ = _dedup_anomaly(tmp, dry=True)
        check("B5 dry=True 一个都不删", len(list(tmp.glob("*.png"))), 8)
        check("B5b dry 仍报出待删数 = 3", n_dry, 3)
        check("B5c dry 也报出会释放多少字节", b_dry > 0, True)

        n, freed, _ = _dedup_anomaly(tmp, dry=False)
        left = sorted(p.name for p in tmp.glob("*.png"))
        check("B1+B2 共删 3 张 (同日 2 + 跨天日 1)", n, 3)
        check("B1 同一天 3 张同画面只留最早那张",
              ("anomaly_101010_t1.png" in left, "anomaly_101020_t2.png" in left,
               "anomaly_101030_t3.png" in left), (True, False, False))
        check("B2 跨天各留一张: 两天的代表都还在",
              ("anomaly_101010_t1.png" in left, "anomaly_120000_t4.png" in left), (True, True))
        check("B2b 跨天那天的重复被删", "anomaly_120100_t5.png" in left, False)
        check("B3 同一天的不同画面一张不删", "anomaly_101100_other.png" in left, True)
        check("B4 非 anomaly_*.png 不碰", (tmp / "reward_like.png").exists(), True)
        check("B6 坏图保留 (读失败绝不删)", "anomaly_101200_broken.png" in left, True)
        check("B* 释放字节 > 0", freed > 0, True)

        n2, _, _ = _dedup_anomaly(tmp, dry=False)
        check("B7 幂等: 再跑一次 = 0 删除", n2, 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ C 段
def sec_c():
    print("C 段 — 运行时闸门 Battler._save_anomaly")
    tmp = Path(tempfile.mkdtemp(prefix="sjrun_"))
    try:
        a, b = frame("A"), frame("B")
        shots = {"img": a}
        logs: list[tuple[str, str]] = []

        # object.__new__ 绕过 __init__: 本用例只碰 _save_anomaly, 不需要设备/OCR
        bb = object.__new__(Battler)
        bb.cfg = types.SimpleNamespace(capture_dir=str(tmp))
        bb.log = lambda lvl, msg: logs.append((lvl, msg))
        bb._shot_img = lambda: shots["img"]

        def n_files():
            return len(list(tmp.glob("anomaly_*.png")))

        bb._save_anomaly("first")
        check("C1 首帧落盘", n_files(), 1)
        check("C1b 日志记 '已存'", any("异常现场已存" in m for _, m in logs), True)

        logs.clear()
        bb._save_anomaly("same_screen")
        check("C2 同画面第二张不落盘", n_files(), 1)
        check("C2b 日志改记 '不重复留图'", any("不重复留图" in m for _, m in logs), True)
        check("C2c 走 info 级 (不是 warn, 免得刷红)",
              [lvl for lvl, m in logs if "不重复留图" in m], ["info"])

        logs.clear()
        shots["img"] = b
        bb._save_anomaly("other")
        check("C3 换画面 -> 落盘, 共 2 张", n_files(), 2)

        logs.clear()
        shots["img"] = a
        bb._save_anomaly("back_to_first")
        check("C4 回到第 1 个画面 -> 仍跳过 (比的是集合)",
              (n_files(), any("不重复留图" in m for _, m in logs)), (2, True))

        # C5 拿不到指纹的畸形帧: 2 通道灰度 (cvtColor 会抛) 但 imencode 仍能写
        shots["img"] = np.full((900, 1600), 128, np.uint8)
        logs.clear()
        bb._save_anomaly("odd_shape")
        check("C5 拿不到指纹时保守落盘 (不误丢现场)",
              (n_files(), any("异常现场已存" in m for _, m in logs)), (3, True))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ D 段
def sec_d():
    print("D 段 — 真实帧: 不同页面必须判成不同画面")
    files = {"转盘": REF / "promo_wheel.jpg", "背包": REF / "bag_panel.jpg",
             "主城": REF / "city_clean.jpg"}
    miss = [k for k, p in files.items() if not p.exists()]
    if miss:
        check(f"D0 真实帧样本齐备 (缺 {miss})", False, True)
        return
    fps = {}
    for k, p in files.items():
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        check(f"D0 {k} 样本可读且 1600x900", (img is not None, img.shape),
              (True, (900, 1600, 3)))
        fps[k] = anomaly_fingerprint(img)
    keys = list(fps)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            k1, k2 = keys[i], keys[j]
            d = diff(fps[k1], fps[k2])
            check(f"D1 {k1} vs {k2} 判不同 (实测差 {d:.1f} ≥ {ANOMALY_DEDUP_DIFF})",
                  same_screen(fps[k1], fps[k2]), False)
    check("D2 同一帧自比判同", same_screen(fps["转盘"], fps["转盘"]), True)


def main() -> int:
    ap = argparse.ArgumentParser(description="异常现场图同画面去重自测")
    ap.parse_args()
    print("===== 异常现场图同画面去重自测 =====")
    sec_a()
    sec_b()
    sec_c()
    sec_d()
    print(f"\n结果: {_n - len(_fails)} 通过 / {len(_fails)} 失败  (共 {_n} 项)")
    for f in _fails:
        print("  FAIL", f)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
