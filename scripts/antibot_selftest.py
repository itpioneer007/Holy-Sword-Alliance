"""防挂机答题解析逻辑自测 (纯算法层, 不连模型/模拟器).

覆盖: _check_antibot 裁 ROI 增强 OCR 命中 / _antibot_lines 行聚合 /
       视觉模型 vision_antibot 真实弹窗端到端 (需 key 可用).
运行: python scripts/antibot_selftest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np

from sj_bot.battler import Battler, make_ocr
from sj_bot import rank_layout as rk

# 用项目统一 OCR 参数 (勿直接 RapidOCR(): 默认配置会把小 ROI 放大到短边 736, 慢 10~50x,
# 既拖垮本自测, 也测不出真实判据路径的耗时)
ocr = make_ocr()

_passed = 0
_failed = 0


def check(name: str, cond: bool):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}")


print("== _check_antibot (裁 ROI + 增强 OCR 命中触发词) ==")
b = Battler.__new__(Battler)
b._ocr = ocr

real_popup = "captures/anomaly_175220_no_battle_ui.png"
import os
if not os.path.exists(real_popup):
    print(f"  ! 跳过: 真实弹窗截图 {real_popup} 不存在")
else:
    img = cv2.imread(real_popup)
    hit = b._check_antibot(img)
    check("真实防挂机弹窗应命中", hit is True)

    # 正常页面不能误伤
    for f, label in [
        ("captures/calib_11_rank_hub.png", "排位主页"),
        ("captures/arena_now.png", "主城/二级菜单"),
        ("captures/calib_00_home.png", "登录主城"),
        ("captures/battle_page.png", "比赛页"),
    ]:
        if not os.path.exists(f):
            continue
        im2 = cv2.imread(f)
        hit2 = b._check_antibot(im2)
        check(f"{label}不误伤", hit2 is False)

print("\n== _is_in_main_city (防挂机答后区分 答错踢主城 vs 答对进入战斗) ==")
# 用途: antibot 答完弹窗消失后, 不再要求主页标题复现 (答对会被匹配光球遮),
# 转而检测主城独有元素 (≥2 个) -> 命中 = 真被踢, 不命中 = 答对继续匹配/战斗.
for f, label, expected in [
    ("captures/calib_00_home.png", "登录主城", True),
    ("captures/calib_11_rank_hub.png", "排位主页", False),
    ("captures/battle_page.png", "战斗页", False),
    ("captures/arena_now.png", "竞技场列表", False),
]:
    if not os.path.exists(f):
        continue
    im = cv2.imread(f)
    is_city = b._is_in_main_city(im)
    check(f"{label} 期望={expected} 实测={is_city}", is_city == expected)
battling = "captures/anomaly_181659_antibot_kicked1.png"
if os.path.exists(battling):
    im = cv2.imread(battling)
    is_city = b._is_in_main_city(im)
    check(f"答题后战斗页 (关键: 不能误判踢主城) 期望=False 实测={is_city}", is_city is False)

print("\n== _antibot_lines 行聚合 (容差=22, 同 y 按钮会合并) ==")
def make_block(text, cx, cy):
    return {"text": text, "cx": cx, "cy": cy, "x0": cx - 40, "y0": cy - 15,
            "x1": cx + 40, "y1": cy + 15, "area": 80 * 30, "score": 0.9}


def lines_case(blocks):
    b2 = Battler.__new__(Battler)
    b2._ocr_texts = lambda img, roi=None, scale=1.0: blocks
    img = np.zeros((900, 1600, 3), np.uint8)
    return b2, b2._antibot_lines(img)


b, rows = lines_case([
    make_block("防挂机问题，请在下方选择答案：2+8=?", 800, 250),
    make_block("4", 350, 340),
    make_block("16", 610, 340),
    make_block("13", 875, 340),
    make_block("10", 1130, 340),
])
check("行数=2 (题干行 + 按钮行)", len(rows) == 2)
check("4 个按钮同行保留独立块", len(rows[1]) == 4)
check("按钮按 x 排序", [x["text"] for x in rows[1]] == ["4", "16", "13", "10"])

print("\n== 本地算术解题 antibot_solver (零成本, 实测弹窗 '2+8=?' 用不到模型) ==")
from sj_bot.antibot_solver import math_answer, solve_math_option

check("2+8=? -> 10", math_answer("防挂机问题，请在下方选择答案：2+8=?") == 10)
check("6x7=? -> 42", math_answer("请计算 6x7=?") == 42)
check("12-4 等于几 -> 8", math_answer("请计算 12-4 等于几") == 8)
check("无算式/无提示词 -> None", math_answer("今天星期几") is None)
check("多式不一致 -> None (交模型)", math_answer("1+1=2 且 2+2=?") is None)


def solver_items(rows):
    return [{"text": t, "cx": cx, "cy": cy} for (t, cx, cy) in rows]


sol = solve_math_option(solver_items([
    ("防挂机问题，请在下方选择答案：2+8=?", 600, 100),
    ("4", 200, 259), ("16", 401, 259), ("13", 600, 259), ("10", 800, 259)]))
check("解题定位按钮10 @ ROI(800,259)", sol == (800, 259))
check("答案不在候选 -> None (交模型)", solve_math_option(solver_items([
    ("防挂机问题，请在下方选择答案：2+8=?", 600, 100),
    ("4", 200, 259), ("16", 401, 259)])) is None)
check("题干里的数字(按钮带外)不误选", solve_math_option(solver_items([
    ("8", 620, 100), ("防挂机问题，请在下方选择答案：2+8=?", 600, 100),
    ("4", 200, 259), ("10", 800, 259)])) == (800, 259))

print("\n== 视觉模型 vision_antibot (需 DeepSeek key) ==")
if os.environ.get("SJ_LLM_SELFTEST") != "1":
    print("  ! 跳过: 每次真实调用都会消耗 token (人民币)。确需验证时设置环境变量")
    print("    SJ_LLM_SELFTEST=1 再运行, 且注意顶部今日模型调用计数会 +1。")
else:
    try:
        from sj_bot.llm_client import LLMClient, LLMError, encode_cv_image_for_llm
        c = LLMClient()
        if os.path.exists(real_popup):
            im = cv2.imread(real_popup)
            x0, y0, x1, y1 = rk.ANTIBOT_ROI
            crop = im[y0:y1, x0:x1]
            b64 = encode_cv_image_for_llm(crop, max_side=800)  # 压缩后发送 (省 token)
            idx = c.vision_antibot(b64, n_options=4)
            check(f"vision_antibot 端到端返回 idx=4 (2+8=10 -> 按钮4)", idx == 4)
        else:
            print("  ! 跳过: 真实弹窗截图不存在")
    except LLMError as e:
        print(f"  ! 跳过视觉模型测试 (LLM 不可用/预算限制): {e}")
    except Exception as e:
        print(f"  ! 跳过视觉模型测试: {e}")

print("\n== 弹窗按钮坐标几何一致性 (硬编码 vs OCR 实测, 防漂移) ==")
# 历史教训: 17:52 / 18:03 两次填常量都没人验证坐标是否真的对得上弹窗, 导致 y 偏 +119px,
# 4 次点击全落在题干与按钮之间的空白, 引擎一直 antibot_giveup.
# 此处对每张保存的真实弹窗截图: OCR 找按钮行, 转换硬编码到全图绝对坐标, 与实测比对
# 偏差 < 30px 才算通过. 任何偏差 > 30px 立即报错, 强制校准 rank_layout.ANTIBOT_OPTION_COORDS.

def measure_buttons(path):
    """OCR 弹窗 ROI 找 4 个数字按钮中心, 全图绝对坐标, 按 x 排序."""
    img = cv2.imread(path)
    if img is None:
        return None
    x0, y0, x1, y1 = rk.ANTIBOT_ROI
    crop = img[y0:y1, x0:x1]
    crop2 = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    res = ocr(crop2)[0]
    btns = []
    for item in (res or []):
        box = item[0]
        text = item[1][0] if isinstance(item[1], tuple) else item[1]
        if text in ("4", "16", "13", "10"):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = (min(xs) + max(xs)) / 2 / 2 + x0
            cy = (min(ys) + max(ys)) / 2 / 2 + y0
            btns.append((text, cx, cy))
    btns.sort(key=lambda b: b[1])
    return btns


# 硬编码转全图绝对坐标
rx, ry, _, _ = rk.ANTIBOT_ROI
hardcoded_abs = [(cx + rx, cy + ry) for (cx, cy) in rk.ANTIBOT_OPTION_COORDS]
print(f"硬编码全图绝对坐标: {hardcoded_abs}")

# 用所有保存的真实弹窗截图交叉验证
popup_imgs = []
for n in os.listdir("captures"):
    if n.startswith("anomaly_") and n.endswith("_antibot_first.png"):
        popup_imgs.append(f"captures/{n}")
extra = "captures/anomaly_175220_no_battle_ui.png"
if os.path.exists(extra):
    popup_imgs.append(extra)
popup_imgs = [p for p in popup_imgs if os.path.exists(p)]

for path in popup_imgs:
    btns = measure_buttons(path)
    if not btns:
        print(f"  ! {os.path.basename(path)}: 无按钮 OCR 到, 跳过 (弹窗特效可能严重)")
        continue
    if len(btns) == 3:
        # 间距 = (b3.x - b1.x) / 2, 推算第 1 个按钮
        spacing = (btns[2][1] - btns[0][1]) / 2
        inferred_left = (btns[0][0], btns[0][1] - spacing, btns[0][2])
        btns = [inferred_left] + btns
        btns.sort(key=lambda b: b[1])
        print(f"  {os.path.basename(path)}: OCR 到 3 个, 等距推算第 1 个按钮")
    elif len(btns) != 4:
        print(f"  ! {os.path.basename(path)}: OCR 异常 {len(btns)} 个, 跳过")
        continue
    # 比对每个按钮的 x/y 偏差
    max_err = 0
    err_detail = []
    for i, (text, mx, my) in enumerate(btns):
        hx, hy = hardcoded_abs[i]
        dx = abs(hx - mx)
        dy = abs(hy - my)
        max_err = max(max_err, max(dx, dy))
        err_detail.append(f"按钮{i + 1}({text}) 实测({mx:.0f},{my:.0f}) 硬编码({hx},{hy}) 偏差dx={dx:.0f} dy={dy:.0f}")
    if max_err < 30:
        check(f"{os.path.basename(path)} 坐标偏差 ≤30px (max={max_err:.0f})", True)
    else:
        check(f"{os.path.basename(path)} 坐标偏差 > 30px (max={max_err:.0f}) -- 需校准!", False)
        for d in err_detail:
            print(f"      {d}")

# ================================================ 选项按钮动态定位 (2026-09-13 根因修复)
print("\n== _locate_antibot_options 动态定位 (弹窗选项行 y 漂移根因修复) ==")


def _synthetic(y_center: int, bg=(90, 40, 60), btn=(235, 210, 60)):
    """合成弹窗: 深紫背景 + 一条青蓝胶囊按钮行 (BGR). btn 需满足
    B>150 且 G>150 且 R<170 的掩码条件."""
    img = np.zeros((900, 1600, 3), np.uint8)
    img[:] = bg
    img[y_center - 30:y_center + 30, 400:1200] = btn
    return img


_b = Battler.__new__(Battler)
for _y in (448, 457, 478, 519, 612):
    _pts = Battler._locate_antibot_options(_b, _synthetic(_y), 4)
    check(f"合成图 y={_y}: 定位到按钮行且 y 中心误差<=2px",
          _pts is not None and all(abs(p[1] + rk.ANTIBOT_ROI[1] - _y) <= 2 for p in _pts))
    check(f"  合成图 y={_y}: 4 个点 x 均落在按钮行内",
          _pts is not None and all(400 <= p[0] + rk.ANTIBOT_ROI[0] <= 1200 for p in _pts))

check("无青蓝按钮行的图 -> 返回 None (不瞎猜)",
      Battler._locate_antibot_options(_b, np.zeros((900, 1600, 3), np.uint8), 4) is None)
check("n_opts<=0 -> 返回 None", Battler._locate_antibot_options(_b, _synthetic(500), 0) is None)

# 真实弹窗样本: 与"独立颜色掩码真值"比对.
# 截图是 anomaly_<时间戳>_ 文件, 不会被后续运行覆盖 -> 可作稳定回归样本.
import glob  # noqa: E402

_fixtures = sorted(glob.glob("captures/anomaly_*_antibot_first.png"))
_done = 0
_skipped = []
for _path in _fixtures[:8]:
    _img = cv2.imdecode(np.fromfile(_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if _img is None:
        continue
    _x0, _y0, _x1, _y1 = rk.ANTIBOT_ROI
    _c = _img[_y0:_y1, _x0:_x1]
    _m = ((_c[:, :, 0] > 150) & (_c[:, :, 1] > 150) & (_c[:, :, 2] < 170))
    if _m.sum() == 0:
        continue
    _rows = _m.sum(axis=1)
    _ys = np.where(_rows > _rows.max() * 0.5)[0]
    # ---- 样本布局前置校验 (2026-09-22 发现误标样本) ----
    # 反例: captures/anomaly_110530_antibot_first.png 文件名带 antibot_first, **实际内容是
    # 荣耀排位赛主页** (人工看图确认): ANTIBOT_ROI 内那行"青蓝"是右侧的「更多排行」「我的记录」
    # 两个按钮, 于是 4 个点落到 2 个按钮上 -> 覆盖率必然低。这是**样本误标**, 不是定位算法退化,
    # 所以不能靠放松 0.4 阈值来"修"(那会掩盖真实回归), 而要把它识别出来并**显式跳过**。
    # 判据来自实测: 真答题弹窗的选项行是**一整条紧凑 band** (6 个真样本: 行高 60~61,
    # x 跨度恒为 95~904, 占 ROI 宽 0.81, 列段数 1); 误标帧则是散布的 11 段、占宽 0.97。
    _band = _m[_ys.min():_ys.max() + 1]
    _cols = _band.sum(axis=0)
    _occ = np.where(_cols > 0)[0]
    _segs, _prev = 0, False
    for _v in _cols:
        _on = _v > 3
        if _on and not _prev:
            _segs += 1
        _prev = _on
    _span = (int(_occ.max()) - int(_occ.min()) + 1) / (_x1 - _x0)
    if _segs > 2 or _span > 0.90:
        _skipped.append((os.path.basename(_path), _segs, round(_span, 2)))
        continue
    _truth = (int(_ys.min()) + int(_ys.max())) // 2 + _y0
    _pts = Battler._locate_antibot_options(_b, _img, 4)
    _done += 1
    check(f"{os.path.basename(_path)}: 动态 y 对齐真值 {_truth}",
          _pts is not None and abs(_pts[0][1] + _y0 - _truth) <= 2)
    if _pts:
        _cov = [float(_m[max(0, py - 10):py + 11, max(0, px - 80):px + 81].mean())
                for px, py in _pts]
        check(f"  4 点邻域按钮覆盖率均>=0.4 {[round(v, 2) for v in _cov]}",
              all(v >= 0.4 for v in _cov))
# 跳过必须可见: 静默丢样本正是"标签会骗人"这类 bug 的温床
for _name, _s, _sp in _skipped:
    print(f"  ! 跳过(非答题弹窗布局, 疑样本误标): {_name} 列段数={_s} 占ROI宽={_sp}")
if _done == 0:
    print("  ! 未找到可用弹窗样本 (captures/anomaly_*_antibot_first.png), 跳过真实图比对")

_src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "sj_bot", "battler.py")
_src = open(_src_path, encoding="utf-8").read()
check("答题坐标优先取动态定位结果 (opts)", "ox, oy = opts[llm_idx - 1]" in _src)
check("动态定位失败才回退硬编码常量", "opts = list(rk.ANTIBOT_OPTION_COORDS)" in _src)

print(f"\n结果: {_passed} 通过, {_failed} 失败")
sys.exit(1 if _failed else 0)
