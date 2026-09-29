# -*- coding: utf-8 -*-
"""主页判据条带化 (HUB_STRIP_ROI) 与主城底部 tab 快路径 ROI 的正负样本校验。

背景 (2026-09-13 提速):
  `_classify_page` 冷启动实测 45~78s, 是"被踢回主城后重导航 ~88s"的主体。其中
  `_is_rank_hub(fast=False)` 的**全图 OCR** 单次就要 10~23s (实测 hub 帧 23.4s),
  `_is_in_main_city` 的全图 OCR 10.4s。
  但主页独有元素其实只分布在 y≈270~670 这一条带里 (倒计时 y≈301 / 三按钮 y≈470-520 /
  我的记录·更多排行·自动参与 y≈580-627); 主城最稳的"在场证明"是底部 tab 导航条
  (y≈790~880)。把这两处从"全图"改"条带"应当**不丢召回**。

本脚本的作用: 在真实截图上对比「全图判据」与「条带判据」的结论, 证明等价后才改代码。
输出每帧两组结论; 只要 **hub 判定不一致** 就算失败 (宁可保守)。
  ⚠️ 正样本不足时不要下结论: 浏览器/模拟器窗口尺寸变化会让老截图失效 —— 本脚本会检查
     帧尺寸必须为 1600x900, 否则跳过并提示。

用法: python scripts/verify_hub_strip.py
"""
from __future__ import annotations

import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sj_bot.battler import Battler, make_ocr          # noqa: E402
import sj_bot.rank_layout as rk                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "captures")

# (文件名, 期望是否主页)
FRAMES = [
    # ---- 主页正样本 (含"取消匹配"态, 主页独有元素仍在条带内) ----
    ("rank_0_hub.png", True),
    ("rank_hub_clean.png", False),   # 文件名误导: 实为「更多排行」排行榜页 (标题 ROI 空 + 条带全是区服名/分数)
    ("rank_hub_clean2.png", True),
    ("rank_hub_now.png", True),
    ("rank_1_matched.png", True),
    # ---- 关键负样本: 二级菜单里有"荣耀排位赛"按钮, 是最容易误判成主页的页面 ----
    ("rank_sub_1.png", False),
    ("rank_sub_now.png", False),
    # ---- 其它负样本 ----
    ("rank_3_reward.png", False),
    ("rank_4_end.png", False),
    ("rank_5_back.png", False),
    ("rank_2_battle.png", False),
    ("arena_page_now.png", False),
    ("calib_13_arena.png", False),
    ("_diag_after_login2.png", False),     # 主城
    ("_diag_before_job.png", False),       # 登录页
    ("_diag_settle_loop.png", False),      # token 失效弹窗
    ("_diag_now.png", False),              # 模拟器桌面
]

# 候选条带 (含实测 UI 元素分布依据)
HUB_STRIP = rk.HUB_STRIP_ROI
TAB_ROI = rk.MAIN_CITY_TAB_ROI      # 直接引用线上常量, 防止脚本与代码脱钩

# 主城底部 tab 快路径的正/负样本 (2026-09-13 补: 原 90px ROI 静默失效的血案)
MAIN_CITY_POS = [
    "anomaly_152646_main_city_loop.png",
    "anomaly_103234_main_city_rescue1.png",
    "anomaly_144452_main_city_rescue1.png",
]
MAIN_CITY_NEG = [
    "rank_0_hub.png", "rank_hub_clean.png", "rank_hub_clean2.png",
    "anomaly_143634_pve_stage_detected.png",
]

results = []


def load(name):
    p = os.path.join(CAP, name)
    if not os.path.exists(p):
        return None
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def main() -> int:
    b = object.__new__(Battler)
    b._ocr = make_ocr()
    print("帧尺寸须为 1600x900 (老截图若尺寸不符请重拍)\n")
    bad = 0
    for name, exp_hub in FRAMES:
        img = load(name)
        if img is None:
            print(f"  [缺图] {name}")
            continue
        if img.shape[:2] != (900, 1600):
            print(f"  [跳过: 尺寸 {img.shape[:2]}] {name}")
            continue

        def fresh():
            b._ocr_cache = {}
            b._ocr_cache_frame = None

        fresh()
        t = time.time()
        hits_full = b._find_hint(img, rk.HUB_UNIQUE_HINTS, scale=1.0)
        d_full = time.time() - t

        fresh()
        t = time.time()
        hits_strip = b._find_hint(img, rk.HUB_UNIQUE_HINTS, roi=HUB_STRIP, scale=1.0)
        d_strip = time.time() - t

        fresh()
        t = time.time()
        tab_set = {h for it in b._ocr_texts(img, roi=TAB_ROI, scale=1.0)
                   for h in rk.MAIN_CITY_TAB_HINTS if h in it["text"]}
        tab_hits = len(tab_set)
        d_tab = time.time() - t

        v_full = hits_full is not None
        v_strip = hits_strip is not None
        agree = (v_full == v_strip)
        ok = agree and (v_strip == exp_hub)
        if not ok:
            bad += 1
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:32s} 期望hub={str(exp_hub):5s} "
              f"全图={str(v_full):5s}({d_full:4.1f}s) 条带={str(v_strip):5s}({d_strip:4.1f}s) "
              f"一致={agree} | 主城tab命中={tab_hits}({d_tab:.1f}s)"
              f"{'' if hits_full is None else ' 全图命中:' + hits_full['match']}"
              f"{'' if hits_strip is None else ' 条带命中:' + hits_strip['match']}")

    # ---------------- 主城底部 tab 快路径 正负样本 ----------------
    print("\n---- 主城 tab 快路径 (MAIN_CITY_TAB_ROI=%r, MIN_HITS=%d) ----"
          % (TAB_ROI, rk.MAIN_CITY_TAB_MIN_HITS))
    for name, want_city in ([(n, True) for n in MAIN_CITY_POS]
                            + [(n, False) for n in MAIN_CITY_NEG]):
        img = load(name)
        if img is None:
            print(f"  [缺图] {name}")
            continue
        if img.shape[:2] != (900, 1600):
            print(f"  [跳过: 尺寸 {img.shape[:2]}] {name}")
            continue
        b._ocr_cache = {}
        b._ocr_cache_frame = None
        t = time.time()
        got = {h for it in b._ocr_texts(img, roi=TAB_ROI, scale=1.0)
               for h in rk.MAIN_CITY_TAB_HINTS if h in it["text"]}
        d = time.time() - t
        verdict = len(got) >= rk.MAIN_CITY_TAB_MIN_HITS
        # 判据只用于"加速": 正样本必须中(否则白跑), 负样本不许中(否则误判)
        ok = (verdict == want_city)
        if not ok:
            bad += 1
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:42s} 期望主城={str(want_city):5s} "
              f"命中={len(got)}/{len(rk.MAIN_CITY_TAB_HINTS)} ({d:4.1f}s) "
              f"判定={verdict}  {sorted(got)}")

    # ---------------- 扁条 ROI 守卫 (静态) ----------------
    # 血案: 90px 高的 tab ROI 在本模型上恒返回 0 块且不报错 ⇒ 快路径静默失效。
    # 这里把"已用真实正样本验过、允许偏薄"的 ROI 列入白名单; 出现**新的**薄 OCR ROI
    # 就报 FAIL, 强制作者跑 scripts/diag_roi_audit.py 验证。
    thin_ok = {"SESSION_TEXT_ROI", "PVE_TITLE_ROI", "PVE_STAR_ROI",
               "HUB_BTN_ROI", "BACK_ROI",    # 均已在真实正样本上验过可检出
               "X2_ROI"}                     # 纯模板匹配, 不受 det 限制
    try:
        import ast
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "sj_bot", "rank_layout.py"),
            encoding="utf-8").read()
        thin_bad = []
        for node in ast.parse(src).body:
            if not (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Tuple)
                    and isinstance(node.targets[0], ast.Name)):
                continue
            nm = node.targets[0].id
            if "ROI" not in nm or nm in thin_ok:
                continue
            try:
                v = [ast.literal_eval(e) for e in node.value.elts]
            except Exception:
                continue
            if len(v) == 4 and all(isinstance(x, int) for x in v) and v[3] - v[1] < 192:
                thin_bad.append((nm, v[3] - v[1]))
        ok = not thin_bad
        if not ok:
            bad += 1
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  扁条 ROI 守卫: 无未验证的薄 OCR ROI"
              f"{'' if ok else '  → ' + str(thin_bad) + ' (请跑 diag_roi_audit.py)'}")
    except Exception as e:      # 守卫本身不该拖垮验证
        print(f"[warn] 扁条守卫执行异常: {e}")

    print(f"\n{sum(results)}/{len(results)} 通过" + (f"  (不一致/期望不符 {bad} 帧)" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
