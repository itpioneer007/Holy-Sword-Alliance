# -*- coding: utf-8 -*-
"""P2 三值模板门控验证 (2026-09-13)。

被验证的对象是 `Battler._hint_or_tpl` 的**判据逻辑** (end/back 两个固定样式按钮):
    score >= HI -> True  (跳过 OCR)
    score <= LO -> False (跳过 OCR)
    中间        -> OCR 兜底

本脚本把**真实帧 + 模拟退化变体**全过一遍, 断言两件必须成立的事:
  1. **不漏判**: 任何真正的 end / back 页, 门控结果必须是 True
     (要么直接命中, 要么落进中间区间交给 OCR —— 绝不能落进 "<= LO" 被判 False)。
  2. **不误判**: 任何非 end / back 页, 门控结果必须是 False
     (要么直接否决, 要么落进中间区间交给 OCR —— 绝不能因高分被判 True)。

同时统计 "会跳过 OCR 的帧数 / 需要 OCR 兜底的帧数", 这是提速收益的直接来源。

用法:
    python scripts/verify_tpl_gate.py            # 纯评分, 不跑 OCR (~1s)
    python scripts/verify_tpl_gate.py --time     # 额外实测 END/BACK ROI 的 OCR 耗时 (~10s)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from sj_bot import rank_layout as rk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "captures")
ASSETS = os.path.join(ROOT, "assets")

# 门控配置: (模板, ROI, HI, LO)
GATES = {
    "end":    ("end_btn.png",          rk.END_ROI,    rk.END_TPL_HI,    rk.END_TPL_LO),
    "back":   ("back_btn.png",         rk.BACK_ROI,   rk.BACK_TPL_HI,   rk.BACK_TPL_LO),
    "reward": ("reward_pick_text.png", rk.REWARD_ROI, rk.REWARD_TPL_HI, rk.REWARD_TPL_LO),
}

# (帧, 真值): "end"/"back"/"reward" = 该页正样本; "-" = 负样本 (三种模板都应判否)
# ⚠️ 真值以**看图核实**为准, 文件名不可信 (本目录已三次抓到标签撒谎):
#   - anomaly_104531_no_end.png 名为 no_end 实为 end 页;
#   - anomaly_121832/141443_*_reward.png 是 token 弹窗**遮挡**宝箱文案 —— 谓词期望值
#     确实是 False (OCR 同样读不到被遮文字, 门控与旧行为一致), 且这两帧由更早的
#     session 判据接管, 故标 "-" 而非 "reward";
#   - rank_stage4_box3.png 是开箱后的「宝箱奖励」弹窗 (含比赛结束按钮), 属 end 链。
CASES = [
    ("rank_4_end.png", "end"),
    ("anomaly_104531_no_end.png", "end"),          # 文件名骗人, 实为 end 页
    ("rank_5_back.png", "back"),
    ("rank_3_reward.png", "reward"),
    ("anomaly_111325_no_end.png", "-"),            # 荣耀排行榜
    ("_p1_after.png", "-"),
    ("rank_0_hub.png", "-"),
    ("rank_hub_now.png", "-"),
    ("rank_1_matched.png", "-"),
    ("_p0_baseline.png", "-"),
    ("anomaly_152646_main_city_loop.png", "-"),
    ("rank_2_battle.png", "-"),
    ("rank_hub_clean.png", "-"),                   # 负样本最高分帧 (end 0.491)
    ("rank_sub_now.png", "-"),
    ("anomaly_143634_pve_stage_detected.png", "-"),
    ("rank_stage4_box3.png", "end"),               # 看图核实: 宝箱奖励弹窗含可点的比赛结束按钮,
                                                   # 引擎此刻就该点结束 ⇒ 实为 end 正样本
                                                   # (曾误标 "-" 使 end 负上界虚高到 1.000)
    ("anomaly_121044_settle_loop_reward.png", "-"),  # token 弹窗压战斗页
    ("anomaly_121832_session_settle_reward.png", "-"),  # token 遮挡宝箱文案
    ("anomaly_141443_settle_loop_reward.png", "-"),     # 同上
    ("_arena_live2.png", "-"),                     # arena 对战列表
]

POS_FRAMES = [("rank_4_end.png", "end_btn.png", rk.END_ROI),
              ("anomaly_104531_no_end.png", "end_btn.png", rk.END_ROI),
              ("rank_5_back.png", "back_btn.png", rk.BACK_ROI),
              ("rank_3_reward.png", "reward_pick_text.png", rk.REWARD_ROI)]

# 模板名 -> 门控键 (退化测试用)
TPL_KEY = {t: k for k, (t, _r, _h, _l) in GATES.items()}

FAILS = []


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILS.append(msg)


def load(path):
    if not os.path.exists(path):
        return None
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def tpl_of(fname):
    img = load(os.path.join(ASSETS, fname))
    # 必须转灰度: _tpl_score 内部对模板与帧都做 COLOR_BGR2GRAY (单通道匹配)。
    return None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def raw_score(img, tpl, roi):
    """与 Battler._tpl_score 同一算法 (灰度 + TM_CCOEFF_NORMED + NaN 剔除)。"""
    if img is None or tpl is None:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = roi
    region = g[y0:y1, x0:x1]
    if region.shape[0] < tpl.shape[0] or region.shape[1] < tpl.shape[1]:
        return None
    _, mv, _, _ = cv2.minMaxLoc(cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED))
    return float(mv) if np.isfinite(mv) else None


def gate_verdict(score, hi, lo):
    """复刻 _hint_or_tpl 的三值语义。返回 'hit' / 'miss' / 'ocr'。"""
    if score is not None:
        if score >= hi:
            return "hit"
        if score <= lo:
            return "miss"
    return "ocr"


def variants(img):
    """模拟现场退化 (与 diag_tpl_degrade.py 同族, 覆盖光照/模糊/压缩/噪声)。"""
    out = [("原图", img)]
    for b in (-60, -40, -25, 25, 40, 60):
        out.append((f"亮度{b:+d}", cv2.convertScaleAbs(img, alpha=1.0, beta=b)))
    for a in (0.7, 0.85, 1.15, 1.3):
        out.append((f"对比度x{a}", cv2.convertScaleAbs(img, alpha=a, beta=0)))
    for k, s in ((3, 0.8), (5, 1.2), (7, 1.6)):
        out.append((f"高斯模糊{k}", cv2.GaussianBlur(img, (k, k), s)))
    for q in (75, 55, 35):
        enc, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if enc:
            out.append((f"JPEG q{q}", cv2.imdecode(buf, cv2.IMREAD_COLOR)))
    rng = np.random.default_rng(7)
    for sig in (6, 12, 20):
        noisy = np.clip(img.astype(np.int16)
                        + rng.normal(0, sig, img.shape).astype(np.int16),
                        0, 255).astype(np.uint8)
        out.append((f"噪声σ{sig}", noisy))
    comb = cv2.convertScaleAbs(img, alpha=0.85, beta=30)
    comb = cv2.GaussianBlur(comb, (5, 5), 1.2)
    enc, buf = cv2.imencode(".jpg", comb, [cv2.IMWRITE_JPEG_QUALITY, 55])
    if enc:
        out.append(("组合退化", cv2.imdecode(buf, cv2.IMREAD_COLOR)))
    return out


def main():
    measure_ocr = "--time" in sys.argv
    print("=" * 96)
    print("P2 三值模板门控验证   门控: score>=HI -> 命中 | score<=LO -> 未命中 | 中间 -> OCR")
    for k, (t, _roi, hi, lo) in GATES.items():
        print(f"    {k:5s} {t:14s} HI={hi:.2f} LO={lo:.2f}")
    print("=" * 96)

    # ---------------- 1. 真实帧: 门控结论必须与真值一致 ----------------
    KEYS = list(GATES.keys())
    print(f"\n[1] 真实帧判定 ({len(CASES)} 帧)")
    hdr = f"    {'帧':44s}{'真值':8s}" + "".join(f"{k:>16s}" for k in KEYS)
    print(hdr)
    tpls = {k: tpl_of(t) for k, (t, _r, _h, _l) in GATES.items()}
    skip_ocr = 0
    need_ocr = 0
    # 实时累积正/负样本分数, 供 [3] 计算安全余量 (不硬编码, 防数字漂移)
    pos_scores = {k: [] for k in KEYS}
    neg_scores = {k: [] for k in KEYS}
    for fn, truth in CASES:
        img = load(os.path.join(CAP, fn))
        if img is None:
            print(f"    [缺图] {fn}")
            continue
        cells = []
        for key in KEYS:
            _t, roi, hi, lo = GATES[key]
            sc = raw_score(img, tpls[key], roi)
            v = gate_verdict(sc, hi, lo)
            want = (truth == key)
            if sc is not None:
                (pos_scores if want else neg_scores)[key].append(sc)
            if v == "ocr":
                need_ocr += 1
                label = f"{sc:.3f}→OCR" if sc is not None else "→OCR"
            else:
                skip_ocr += 1
                label = f"{sc:.3f}→{'T' if v == 'hit' else 'F'}"
                # 只有直接定论的帧才需要断言; OCR 帧由 OCR 兜底负责
                if v == "hit" and not want:
                    FAILS.append(f"{fn} 误判: {key} 门控=命中, 但真值不是 {key}")
                if v == "miss" and want:
                    FAILS.append(f"{fn} **漏判**: {key} 门控=未命中, 但真值是 {key}页")
            cells.append(f"{label:>16s}")
        print(f"    {fn:44s}{truth:8s}" + "".join(cells))
    print(f"\n    直接定论 (跳过 OCR): {skip_ocr} 项 | 需 OCR 兜底: {need_ocr} 项")
    ok(need_ocr == 0, f"全部真实帧均可直接定论, OCR 回退 {need_ocr} 次 "
                      f"(中间区间为空 ⇒ 零回退)")

    # ---------------- 2. 退化正样本: 不得出现"静默漏判" ----------------
    print("\n[2] 正样本退化压力测试 (每个正样本 20 种退化, 检查是否跌破 LO)")
    print("    漏判 = 真按钮因退化掉到 LO 以下, 门控判未命中且不再跑 OCR")
    for fn, tname, roi in POS_FRAMES:
        key = TPL_KEY[tname]
        _t, _roi, hi, lo = GATES[key]
        img = load(os.path.join(CAP, fn))
        if img is None or img.shape[:2] != (900, 1600):
            print(f"    [跳过] {fn}")
            continue
        scores = []
        for name, v in variants(img):
            sc = raw_score(v, tpls[key], roi)
            if sc is not None:
                scores.append((sc, name))
        scores.sort()
        worst_s, worst_n = scores[0]
        pos_scores[key].extend(s for s, _n in scores)
        silent = [n for s, n in scores if s <= lo]
        print(f"    {fn:34s} 模板 {tname:12s} 最低分 {worst_s:.3f} ({worst_n})  "
              f"低于 LO={lo:.2f} 的退化: {silent if silent else '无'}")
        ok(not silent, f"{fn}: 无退化跌破 LO ⇒ 门控不会静默漏判 ({len(scores)} 个变体)")

    # ---------------- 3. 双阈值区间的实测余量 (由 [1][2] 实测数据推导) ----------------
    print("\n[3] 阈值安全余量 (实测推导: 负样本上界来自 [1], 最差正样本来自 [2])")
    for key in KEYS:
        _t, _roi, hi, lo = GATES[key]
        if not neg_scores[key] or not pos_scores[key]:
            print(f"    {key}: 样本不足, 跳过")
            continue
        n = max(neg_scores[key])
        w = min(pos_scores[key])
        print(f"    {key:5s} HI={hi:.2f} 高于负样本上界 {hi - n:+.3f} | "
              f"LO={lo:.2f} 高于负样本上界 {lo - n:+.3f}, 低于最差正样本 {w - lo:+.3f}")
        print(f"          (负样本上界 {n:.3f} / 最差正样本 {w:.3f}, "
              f"样本 {len(neg_scores[key])} 负 / {len(pos_scores[key])} 正)")
        ok(hi > n, f"{key}: HI 高于全部负样本 (不误判)")
        ok(lo < w, f"{key}: LO 低于全部正样本含退化 (不漏判)")
        ok(lo > n, f"{key}: LO 高于负样本上界 ⇒ 稳态帧能直接否决, 真正省下 OCR")

    # ---------------- 4. (可选) 实测这些 ROI 的 OCR 成本 ----------------
    if measure_ocr:
        print("\n[4] 实测各 ROI 的 OCR 耗时 (被门控省掉的就是这部分)")
        try:
            from sj_bot.battler import make_ocr
            ocr = make_ocr()
            probe = load(os.path.join(CAP, "rank_0_hub.png"))  # 主页帧 = 典型负样本
            if probe is not None:
                for key in KEYS:
                    _t, roi, _hi, _lo = GATES[key]
                    x0, y0, x1, y1 = roi
                    crop = probe[y0:y1, x0:x1]
                    t0 = time.time()
                    ocr(crop)
                    dt = time.time() - t0
                    h = y1 - y0
                    w = x1 - x0
                    print(f"    {key:5s} ROI {w}x{h}  单次 OCR {dt:.2f}s")
        except Exception as e:                                     # pragma: no cover
            print(f"    [跳过] OCR 实测失败: {e}")

    print("\n" + "=" * 96)
    if FAILS:
        print(f"结果: ✗ {len(FAILS)} 项失败")
        for f in FAILS:
            print("   - " + f)
        return 1
    print("结果: ✓ 全部通过 (真实帧 0 误判 / 0 漏判, 退化正样本 0 静默漏判)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
