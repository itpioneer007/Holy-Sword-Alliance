# -*- coding: utf-8 -*-
"""_classify_page 页面识别自测 (2026-09-09 结算自愈前置):
mock 底层检测器, 验证判定顺序与返回值; 不依赖 adb/OCR."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from sj_bot.battler import Battler
from sj_bot import rank_layout as rk

IMG = np.zeros((900, 1600, 3), np.uint8)

def make(hits: dict):
    """hits: 哪些检测器命中 -> 返回配置好的 Battler 实例"""
    b = object.__new__(Battler)
    def finder(name):
        def f(*a, **k):
            return {"cx": 0, "cy": 0} if name in hits else None
        return f
    b._find_hint = finder("hint:" + "*")  # 下面按 hints 首词区分
    def find_hint(img, hints, roi=None, scale=1.0):
        for h in hints:
            if f"hint:{h}" in hits:
                return {"cx": 0, "cy": 0}
        return None
    b._find_hint = find_hint
    def find_btn_tpl(img, fname, roi, min_score=0.0):
        # 战斗页双锚点: x2_btn.png 单独一个 key, 其余模板统一用 "tpl"
        if fname.startswith("x2"):
            return {"cx": 0, "cy": 0} if "x2" in hits else None
        return {"cx": 0, "cy": 0} if "tpl" in hits else None
    b._find_btn_tpl = find_btn_tpl
    def tpl_score(img, fname, roi):
        # P2 门控 (2026-09-13): end/back/reward 走 _tpl_score, mock 必须一起注入。
        # 按键区分: "tpl:end" / "tpl:back" / "tpl:reward" 表示"该模板高分命中";
        # 未列出的模板返回 0.0 = 低分。低分与高分**都会跳过 OCR** (这正是提速来源)。
        # ⚠️ reward 必须独立成键: 曾把它归入 back 桶, "返回大厅页"用例的 tpl:back
        #    会让 reward 门控误中 (BACK_HI 0.82 ≥ REWARD_HI 0.75) -> 判成 reward。
        if fname.startswith("reward"):
            return rk.REWARD_TPL_HI + 0.1 if "tpl:reward" in hits else 0.0
        key = "tpl:end" if fname.startswith("end") else "tpl:back"
        hi = rk.END_TPL_HI if fname.startswith("end") else rk.BACK_TPL_HI
        return hi + 0.1 if key in hits else 0.0
    b._tpl_score = tpl_score
    b._find_skip_btn = finder("skip")
    b._is_rank_hub = lambda img, fast=False: "hub" in hits
    b._is_in_main_city = lambda img: "main_city" in hits
    b._shot_img = lambda: IMG
    return b

def run(name, hits, expect):
    b = make(set(hits))
    got = b._classify_page()
    ok = got == expect
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got} (期望 {expect})")
    return ok

results = [
    run("每日上限弹窗优先", ["hint:请明日再来", "hint:返回大厅"], "daily_limit"),
    run("冷却弹窗次之", ["hint:请休息15分钟", "tpl"], "cooldown"),
    run("宝箱页", ["hint:请选择一个宝箱", "tpl:reward", "skip"], "reward"),
    run("比赛结束页", ["tpl:end", "hint:返回大厅"], "end"),
    run("返回大厅页", ["tpl:back", "skip", "hub"], "back"),
    # ⚠️ P2 有意取舍 (2026-09-13): 模板**低分**时门控直接判否, 不再回退 OCR ⇒
    #    若出现"模板不匹配但文本仍可读"(仅当游戏改版换按钮美术)会被判成 hub。
    #    实测当前资产下该组合不可达: 真实 end 页 0.999/1.000、back 页 1.000, 均 ≥ HI。
    #    这条用例锁死该取舍 —— 别再把它当 bug"修"回 OCR 兜底, 那会重新引入 13.6s/局的 OCR。
    #    失效信号: _wait_btn 会打"未命中模板, 改用 OCR 兜底"告警; 复核跑 verify_tpl_gate.py。
    run("模板低分且文本可读 -> 判 hub (P2 取舍, 非 bug)", ["hint:返回大厅", "hub"], "hub"),
    # 战斗页判定 = 「跳过」+「X2」双锚点 (单靠跳过模板会误判擂台页"创建擂台"按钮)
    run("战斗页(跳过+X2 双锚点)", ["skip", "x2", "hub"], "battle"),
    run("战斗页(OCR 兜底读到跳过)", ["hint:跳过", "hub"], "battle"),
    run("仅跳过模板命中(擂台页场景) 不算战斗页", ["skip", "hub"], "hub"),
    # ⚠️ 顺序: hub 必须在 main_city **之前**。曾试过把主城 tab 条带提前短路 (P1b),
    # 实测净负 (本函数调用场景以主页帧为主, 而 tab 条带在主页上要 10~22s) 已回退。
    # 这条用例就是锁死"hub 优先于 main_city"的防线, 别再把它当作待优化项。
    run("主页优先于主城判定 (顺序锁, P1b 已回退)", ["hub", "main_city"], "hub"),
    run("排位主页", ["hub"], "hub"),
    run("主城", ["main_city"], "main_city"),
    # 顺序约束: 会话弹窗是半透明遮罩, 必须最先 (不能被任何"快路径"吞掉)
    run("会话弹窗盖在主城上: session 仍优先", ["hint:token失效", "main_city"], "session"),
    run("全不命中", [], "unknown"),
]

# ============================================================ 第 2 段: 真实 _is_in_main_city
# ⚠️ 上面全部用例都把 `_is_in_main_city` **整个 mock 掉**了 (见 make() 第 46 行), 因此
#    **测不到它内部**。而 2026-09-23 新增的「设置浮层」守卫正好在它内部
#    (tab 快路径之后 / 全图回退之前)。所以这里用**真实实现 + 桩化 OCR** 单独锁死:
#    不依赖 adb, 也不真跑 OCR, 只验证判定顺序与阈值。
#
# 回归背景 (真机实测): 设置浮层是半透明的, 其下主城建筑文字 (荣誉殿堂/铁匠铺/王者之巅)
# 仍能被 OCR 读出 ⇒ 全图回退会命中 ≥2 个 _MAIN_CITY_HINTS ⇒ 误判 'main_city'
# ⇒ 引擎去点被遮住的「王者之巅」⇒ 盲点无效 ⇒ nav_failed, 且因非 'unknown' 逃生分支不触发。
def _blocks(txts):
    return [{"text": t, "cx": 0, "cy": 0, "score": 1.0, "area": 1} for t in txts]

TAB_OK = _blocks(rk.MAIN_CITY_TAB_HINTS)                       # 真主城底部 tab (5/5)
MENU_FULL = _blocks(rk.MENU_HINTS)                             # 浮层六宫格 (6/6)
CITY_BEHIND = _blocks(("荣誉殿堂", "铁匠铺", "王者之巅"))        # 浮层下仍可读的主城建筑

def make_real(tab, menu, full):
    b = object.__new__(Battler)
    def ocr_texts(img, roi=None, scale=1.0):
        if roi == rk.MAIN_CITY_TAB_ROI:
            return tab
        if roi == rk.MENU_ROI:
            return menu
        if roi is None:
            return full
        return []
    b._ocr_texts = ocr_texts
    b._shot_img = lambda: IMG
    b.log = lambda lv, msg: None
    return b

def run_real(name, tab, menu, full, expect):
    got = make_real(tab, menu, full)._is_in_main_city(IMG)
    ok = got == expect
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got} (期望 {expect})")
    return ok

results += [
    run_real("真主城: tab 快路径命中 -> 主城", TAB_OK, MENU_FULL, [], True),
    # ★ 核心顺序锁: 浮层下全图能读出 3 个主城词, 但守卫必须**先**拦下 -> 非主城
    run_real("设置浮层: tab 0 块 + 浮层 6/6 -> 非主城 (守卫早于全图回退)",
             [], MENU_FULL, CITY_BEHIND, False),
    run_real("tab 未命中且无浮层 -> 仍走全图回退判主城 (召回未降级)",
             [], [], CITY_BEHIND, True),
    run_real("浮层只命中 1 词 (<MIN_HITS) -> 不误判浮层, 仍按全图判主城",
             [], MENU_FULL[:1], CITY_BEHIND, True),
]
print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
