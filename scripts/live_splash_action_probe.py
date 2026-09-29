# -*- coding: utf-8 -*-
"""真机实验: 「契约战令」开场页该用 back 键还是点击才能进游戏 (09-23 用户需求).

背景: 点登录页「开始」后, 游戏先进一个全屏活动开场页 (底部绿字「点击任意位置进入游戏」)。
实测 118s 无自动消失。`_recover_from_session` 目前对未识别页面只按 **back 键** (最多 2 次),
需要确认这条路径到底有没有用。

方法: 每个动作后连拍, 用**与起始帧的像素差**判断页面是否变了 (便宜, ~0.1s),
只在"页面真的变了"时才跑 OCR 分类 —— 全图 OCR 单次 10~25s, 每步都 OCR 会把实验拖到几分钟。

实验设计:
  step1 back x1 / step2 back x2 -> 页面不变则说明 back 对开场页无效
  step3 tap 屏幕中央 x1        -> 若进入游戏, 说明正确动作是 tap

用法: python scripts/live_splash_action_probe.py
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.battler import Battler            # noqa: E402

OUT = ROOT / "outputs"
REF = None


def save(img, name: str) -> None:
    try:
        cv2.imencode(".png", img)[1].tofile(str(OUT / f"probe_{name}.png"))
    except Exception as e:
        print(f"  ⚠ 存图异常 {name}: {e}")


def small(img) -> np.ndarray:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (160, 90)).astype(np.float32)


def snap(b: Battler, name: str):
    img = b._shot_img()
    if img is None:
        print(f"  [{name}] 截图失败")
        return None
    save(img, name)
    return img


def step(b: Battler, name: str, label: str) -> float:
    """拍一帧, 返回与起始帧的像素差。"""
    global REF
    img = snap(b, name)
    if img is None:
        return -1.0
    d = float(np.abs(small(img) - REF).mean())
    changed = "页面已变" if d > 2.0 else "页面未变"
    print(f"  [{label}] {name}  diff_vs起始={d:6.2f}  -> {changed}")
    return d


def main() -> None:
    global REF
    b = Battler(mode="rank", rounds=1)
    b.log = lambda lv, msg: print(f"  [{lv}] {msg}")

    img = snap(b, "s0_start")
    if img is None:
        return
    REF = small(img)
    print(f"起始帧 probe_s0_start.png (尺寸 {img.shape[:2]})")
    print("  人工确认: 「契约战令」开场页, 底部绿字「点击任意位置进入游戏」")

    print("\n=== step1: 按 1 次系统返回键 ===")
    print(f"  _press_back() -> {b._press_back()}")
    time.sleep(4)
    step(b, "s1_after_back1", "back x1")

    print("\n=== step2: 再按 1 次返回键 (累计 2 次) ===")
    print(f"  _press_back() -> {b._press_back()}")
    time.sleep(4)
    step(b, "s2_after_back2", "back x2")

    print("\n=== step3: 点击屏幕中央 (点击任意位置进入游戏) ===")
    cx, cy = 800, 450
    b._tap(cx, cy)
    print(f"  _tap({cx},{cy})")
    for i in range(1, 8):
        time.sleep(3)
        d = step(b, f"s3_tap_{i:02d}", f"tap t+{i*3}s")

    print("\n完成。逐帧图: outputs/probe_s*.png")


if __name__ == "__main__":
    main()
