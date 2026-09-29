"""OCR 配置对照基准 (2026-09-13).

背景 (用户反馈"点击单人匹配迟到 46-60s, 倒计时已归零 -> 必被踢回主城"):
  实测 RapidOCR 单次 ROI OCR 需 3~30s (异常). 根因 = rapidocr_onnxruntime 1.2.3
  默认配置 `Det.limit_type: min` + `Det.limit_side_len: 736`:
  短边 < 736 的图会被**放大**到短边 736 ——
      160x60   -> 1963x736  (面积 x12)
      670x120  -> 4108x736  (面积 x38)
      1000x220 -> 3345x736
  于是小 ROI 反而比全图 (1600x900, 短边 900 > 736 不缩放) 更贵, 单局串行 8 次
  OCR 合计 60s+ -> 点"单人匹配"时倒计时早已归零.

对照三档配置 (见 CONFIGS):
  A 现用默认          limit_type=min, limit_side_len=736   (会放大所有小 ROI)
  B 仅降不升          limit_type=max, limit_side_len=1632  (全图 1600x900 行为与 A 完全一致,
                                                            小 ROI 不再放大)
  C 轻度放大          limit_type=min, limit_side_len=320   (保留部分放大能力, 成本降约 5x)

判定口径 (两条都要满足才算可用):
  ① 提速: 各 case 的 wall 时间显著下降;
  ② 不丢判据: 同一 ROI 上项目既有 hints 的命中结果与 A 档**完全一致**
     (只测速度不测召回 = 制造漏判, 是本项目 09-12 已踩过的坑).

用法:
  python scripts/ocr_config_bench.py            # 全部 case
  python scripts/ocr_config_bench.py --quick    # 只跑关键 case
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot import rank_layout as rk  # noqa: E402

CAP = ROOT / "captures"

# 配置: (标签, RapidOCR kwargs)
# 注意 rapidocr 1.2.3 的 UpdateParameters 在收到任何 det_* 参数时都会读
# det_dict['model_path'], 缺 key 直接 KeyError -> 必须显式带 det_model_path=''
# (空串触发回落到包内默认模型路径).
def _det_kwargs(limit_type: str, limit_side_len: int) -> dict:
    return {
        "det_limit_type": limit_type,
        "det_limit_side_len": limit_side_len,
        "det_model_path": "",
    }


CONFIGS = [
    ("A 现用默认 min/736", {}),
    ("B 仅降不升 max/1632", _det_kwargs("max", 1632)),
    ("C 轻度放大 min/320", _det_kwargs("min", 320)),
]

MAIN_CITY_HINTS = (
    "英雄", "装备", "技能", "背包",
    "王者之巅", "矿洞", "英雄城堡", "铁匠铺", "地下城", "藏宝之地",
    "深渊迷宫", "魔龙", "荣誉殿堂",
    "每日任务", "每日挑战", "每日充值", "充值有礼",
)


def _mk_cases():
    """(case 名, 图文件名, roi 或 None=全图, hints) —— 取自项目真实判据路径."""
    hub = "rank_0_hub.png"
    reward = "rank_3_reward.png"
    end = "rank_4_end.png"
    back = "rank_5_back.png"
    battle = "rank_2_battle.png"
    city = "anomaly_103234_main_city_rescue1.png"
    return [
        ("hub 标题 (荣耀排位赛)", hub, rk.HUB_TITLE_ROI, ("荣耀排位赛",), False),
        ("hub 匹配按钮 (单人匹配)", hub, rk.HUB_BTN_ROI, ("单人匹配",), False),
        ("hub 独有元素 (全图)", hub, None, rk.HUB_UNIQUE_HINTS, False),
        ("reward 宝箱文案", reward, rk.REWARD_ROI, rk.REWARD_HINTS, False),
        ("end 比赛结束", end, rk.END_ROI, rk.END_HINTS, False),
        ("back 返回大厅", back, rk.BACK_ROI, rk.BACK_HINTS, False),
        ("battle 跳过/X2", battle, rk.SKIP_ROI, ("跳过", "X2", "x2"), False),
        ("主城独有元素 (全图)", city, None, MAIN_CITY_HINTS, True),
    ]


def _hit(texts, hints, need_multi=False):
    hits = 0
    for t in texts:
        for h in hints:
            if h in t:
                hits += 1
                break
    return hits >= 2 if need_multi else hits >= 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只跑关键 case")
    ap.add_argument("--only", default="", help="只跑指定档位 (按标签首字母, 如 B 或 A,B)")
    args = ap.parse_args()

    from rapidocr_onnxruntime import RapidOCR

    cases = _mk_cases()
    if args.quick:
        keep = ("hub 标题", "hub 匹配按钮", "reward 宝箱文案", "主城独有元素")
        cases = [c for c in cases if c[0].startswith(keep)]

    want = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    picks = [c for c in CONFIGS if not want or c[0][0].upper() in want]
    engines = []
    tot = {}
    for label, kw in picks:
        t0 = time.time()
        engines.append((label, RapidOCR(**kw)))
        tot[label] = 0.0
        print(f"[初始化] {label:22s} {time.time() - t0:.1f}s", flush=True)

    # 缓存图片, 避免重复读盘. 注意 cv2.imread 不支持中文路径 (本项目既有坑),
    # 统一走 np.fromfile + imdecode.
    imgs: dict[str, np.ndarray] = {}

    print(f"\n{'case':26s} | " + " | ".join(f"{lbl[:12]:>14s}" for lbl, _ in engines))
    print("-" * (28 + 18 * len(engines)))
    verdict_ok = True
    ran = 0
    for name, fname, roi, hints, multi in cases:
        if fname not in imgs:
            p = CAP / fname
            img = None
            if p.exists():
                img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8),
                                   cv2.IMREAD_COLOR)
            if img is None:
                print(f"{name:26s} | 图片缺失或解码失败 {fname}")
                continue
            imgs[fname] = img
        img = imgs[fname]
        work = img if roi is None else img[roi[1]:roi[3], roi[0]:roi[2]]
        work = np.ascontiguousarray(work)

        ran += 1
        cells, base = [], None
        for label, eng in engines:
            t0 = time.time()
            try:
                res, _ = eng(work)
            except Exception as e:  # noqa: BLE001
                cells.append(f"{'ERR:' + str(e)[:8]:>14s}")
                continue
            dt = time.time() - t0
            tot[label] = tot.get(label, 0.0) + dt
            texts = [r[1] for r in (res or [])]
            got = _hit(texts, hints, multi)
            tag = "命中" if got else "未命中"
            cells.append(f"{dt:5.1f}s {tag:>6s}")
            if base is None:
                base = got
            elif got != base:
                verdict_ok = False
        mark = "" if all(c.endswith("命中") == base for c in cells if "s " in c) else ""
        print(f"{name:26s} | " + " | ".join(cells) + mark, flush=True)

    print("\n合计 wall: " + " | ".join(f"{lbl[:12]}={tot.get(lbl, 0):.1f}s" for lbl, _ in engines))
    if ran == 0:
        print("\n结论: 没有任何 case 成功执行 (截图缺失?) -> 本次对照无效")
        return 2
    print("\n结论: " + ("各档命中一致 -> 提速档可安全替换" if verdict_ok
                      else "⚠️ 存在命中差异 -> 提速档有漏判风险, 需按 case 复核"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
