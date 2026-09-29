# -*- coding: utf-8 -*-
"""二级菜单入口锚点诊断 (2026-09-13 P3)。

背景: `_enter_pvp_hub(mode='rank')` 的入口点击用的是**写死坐标** `RANK_ENTRY_HOTSPOT=(613,605)`
(注释: "在'排'字下方 70px")。而同一函数里 `mode='arena'` 走的是 OCR 定位 `_tap_hint(("全民争霸",))`
—— 两条分支能力不一致。写死坐标正是用户抱怨的"坐标只能硬编码吗"的典型:
一旦卡片布局漂移(项目里「王者之巅」就实测漂移过 856,577 / 1157,576 / 700,415),
写死的 y=605 就会点空。

本脚本只做**只读诊断 + 一次正常导航点击**(王者之巅 -> 二级菜单), 用来回答三个问题:
  1. 二级菜单上 OCR 实际能读到哪些文字块, 中心坐标各是多少?
  2. 「荣耀排位赛」文字中心与可用热区 (613,605) 的真实偏移是多少? 是否稳定?
  3. 卡片内部有没有**比"文字+固定偏移"更直接的锚点**(例如按钮文字/卡面说明)?

用法: python scripts/diag_submenu_entry.py
输出: captures/_p3_submenu.png (二级菜单原图) + 控制台文本块清单
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2      # noqa: E402

from sj_bot.battler import Battler      # noqa: E402
from sj_bot import rank_layout as rk    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CAP = ROOT / "captures"


def save(img, name):
    p = CAP / name
    cv2.imencode(".png", img)[1].tofile(str(p))
    return p


def dump(img, title, only_roi=None):
    """打印 OCR 文本块: 文本 / 中心 / 面积, 按 y 再 x 排序。"""
    items = b._ocr_texts(img, roi=only_roi, scale=1.0)
    print(f"\n--- {title} ({len(items)} 块) ---")
    for it in sorted(items, key=lambda d: (d["cy"], d["cx"])):
        print(f"    cx={it['cx']:7.1f} cy={it['cy']:7.1f} "
              f"w={it['area'] ** 0.5:6.1f}  {it['text']}")
    return items


b = Battler(mode="rank", rounds=1)
import threading                        # noqa: E402
b.stop_evt = threading.Event()
b.log = lambda lv, msg: print(f"    [{lv}] {msg}")

# ---------------- 1. 当前页面 ----------------
img = b._shot_img()
if img is None:
    print("[X] 截图失败")
    sys.exit(1)
print(f"[img] {img.shape}  当前页 = {b._classify_page(img)}")
print(f"      arena_open={b.cfg is not None}")

# ---------------- 2. 定位并点击「王者之巅」 ----------------
wz = b._find_hint(img, ("王者之巅",), scale=1.0)
if not wz:
    print("[X] 当前不在主城 (未找到「王者之巅」)。请手动把游戏切到主城后重跑。")
    save(img, "_p3_not_city.png")
    sys.exit(2)
bx, by = int(wz["cx"]), int(wz["cy"])
print(f"[1] 王者之巅 文字中心 = ({bx},{by})  (历史实测漂移过 856,577 / 1157,576 / 700,415)")
b._tap(bx, by)

# ---------------- 3. 等二级菜单 ----------------
deadline = time.time() + 12
sub = None
while time.time() < deadline:
    time.sleep(1.5)
    sub = b._shot_img()
    if sub is not None and b._is_pvp_submenu(sub):
        break
    sub = None
if sub is None:
    print("[X] 12s 内未出现二级菜单 (「王者之争」未识别)")
    img2 = b._shot_img()
    if img2 is not None:
        print("    落地页 =", b._classify_page(img2), "->", save(img2, "_p3_submenu_fail.png"))
    sys.exit(3)

p = save(sub, "_p3_submenu.png")
print(f"\n[2] 二级菜单已就位 -> {p}   判定={b._classify_page(sub)}")

# ---------------- 4. dump 文本块 ----------------
items = dump(sub, "二级菜单全图 OCR")

print("\n--- 关键锚点对比 ---")
rank_hit = b._find_hint(sub, rk.PVP_MODE_HINTS["rank"], scale=1.0)
arena_hit = b._find_hint(sub, rk.PVP_MODE_HINTS["arena"], scale=1.0)
print(f"    写死热区 RANK_ENTRY_HOTSPOT = {rk.RANK_ENTRY_HOTSPOT}")
if rank_hit:
    dx = rk.RANK_ENTRY_HOTSPOT[0] - rank_hit["cx"]
    dy = rk.RANK_ENTRY_HOTSPOT[1] - rank_hit["cy"]
    print(f"    「荣耀排位赛」文字中心 = ({rank_hit['cx']:.0f},{rank_hit['cy']:.0f})"
          f"  ⇒ 写死热区相对偏移 = ({dx:+.0f},{dy:+.0f})")
else:
    print("    [!] OCR 未识别到「荣耀排位赛」")
if arena_hit:
    print(f"    「全民争霸」文字中心 = ({arena_hit['cx']:.0f},{arena_hit['cy']:.0f})")
else:
    print("    [!] OCR 未识别到「全民争霸」")

print("\n--- 卡片区域 (y 400~760) 内的所有文本块 ---")
dump(sub, "卡片带", only_roi=(0, 400, 1600, 760))
print("\n完成。请据此判断入口锚点是否可改为 OCR 推导。")
