# -*- coding: utf-8 -*-
"""导航重试 + 战斗页接管 自测 (2026-09-11 P0/P1 根因修复).

覆盖:
  A. _enter_pvp_hub 终点断言放宽: 落点 hub/battle/reward/end/back 视为成功;
     main_city / unknown / daily_limit 仍判失败 (不会把"没进去"当成功放过去).
  B. _enter_pvp_hub_retry 重试语义: 第2/3次成功 / 全失败收口 / 停止可中断 / 首次成功不重试.
  C. 主循环战斗页接管分支存在性 (源码静态断言, 防回归).
不依赖 adb / OCR / 真机.
"""
import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from sj_bot.battler import Battler

IMG = np.zeros((900, 1600, 3), np.uint8)
RESULTS = []


def check(name, got, expect):
    ok = got == expect
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got} (期望 {expect})")
    return ok


# ---------------------------------------------------------------- A. 终点断言
def make_nav(page):
    """构造只走『二级菜单 -> 点入口 -> 终点断言』路径的 Battler."""
    b = object.__new__(Battler)
    b.errors = 0
    b._nav_page = None
    b.taps, b.logs, b.anomalies = [], [], []
    b.stop_evt = threading.Event()
    b._shot_img = lambda: IMG
    b._is_rank_hub = lambda img, fast=False: False     # 步骤1 不在主页 -> 走步骤2
    b._is_pvp_submenu = lambda img: True               # 已在二级菜单
    b._is_city = lambda img: False
    b._is_on_list = lambda img: False
    b._find_hint = lambda img, hints, roi=None, scale=1.0: None
    b._tap = lambda x, y: b.taps.append((x, y))
    b._sleep_stop = lambda s: None
    b._classify_page = lambda img=None: page
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._save_anomaly = lambda tag: b.anomalies.append(tag)
    return b


print("=== A. 导航终点断言放宽 (P0) ===")
for page, expect_ok in [("hub", True), ("battle", True), ("reward", True),
                        ("end", True), ("back", True),
                        ("main_city", False), ("unknown", False),
                        ("daily_limit", False), ("cooldown", False)]:
    b = make_nav(page)
    ok = b._enter_pvp_hub("rank")
    check(f"落点 {page} -> 导航成功={expect_ok}", ok, expect_ok)
    if expect_ok:
        check(f"  落点 {page} 记录到 _nav_page", b._nav_page, page)
        check(f"  落点 {page} 未存异常图", len(b.anomalies), 0)
    else:
        check(f"  落点 {page} 存异常图", b.anomalies, ["rank_hub_failed"])

# ---------------------------------------------------------------- B. 重试语义
def make_retry(seq):
    b = object.__new__(Battler)
    b.stop_evt = threading.Event()
    b.logs = []
    b.sleeps = []
    b.calls = []
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._sleep_stop = lambda s: b.sleeps.append(s)

    def fake(mode):
        b.calls.append(mode)
        i = len(b.calls) - 1
        return seq[i] if i < len(seq) else seq[-1]
    b._enter_pvp_hub = fake
    return b


print("\n=== B. 导航重试语义 (P1) ===")
b = make_retry([False, True])
check("第2次成功 -> 返回 True", b._enter_pvp_hub_retry("rank"), True)
check("  实际尝试次数 = 2", len(b.calls), 2)

b = make_retry([False, False, True])
check("第3次成功 -> 返回 True", b._enter_pvp_hub_retry("rank"), True)
check("  实际尝试次数 = 3", len(b.calls), 3)

b = make_retry([False])          # 永远失败
check("连续失败 -> 返回 False", b._enter_pvp_hub_retry("rank"), False)
check("  尝试次数钳制在 tries=3", len(b.calls), 3)
check("  失败日志含『连续』", any("连续" in m for _, m in b.logs), True)
check("  重试间隔 2.5s", b.sleeps, [2.5, 2.5])

b = make_retry([True])
check("首次成功 -> 返回 True", b._enter_pvp_hub_retry("rank"), True)
check("  不产生多余尝试/等待", (len(b.calls), len(b.sleeps)), (1, 0))

b = make_retry([False])
b.stop_evt.set()
check("已请求停止 -> 立即返回 False", b._enter_pvp_hub_retry("rank"), False)
check("  停止时零次尝试", len(b.calls), 0)

b = make_retry([False, False, True])
check("自定义 tries=2 时第3次不执行", b._enter_pvp_hub_retry("rank", tries=2), False)
check("  尝试次数 = 2", len(b.calls), 2)

# ---------------------------------------------------------------- C. 主循环接管分支
print("\n=== C. 主循环战斗页接管 (静态断言) ===")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "sj_bot", "battler.py"), encoding="utf-8").read()
check("战斗页判定走统一判据", "elif self._is_in_battle(img):" in src, True)
check("结算链检测先于战斗页判定 (宝箱层覆盖在战斗画面之上)",
      src.index("self._pre_settle_stage = None if fast_hub else self._detect_settle_stage(img)")
      < src.index("elif self._is_in_battle(img):"), True)
check("战斗中重导航走统一判据",
      "if self._is_in_battle(img):\n            self._nav_page = \"battle\"" in src, True)
check("第1步短路点匹配", "已在战斗页, 跳过点击匹配" in src, True)
check("第2步直赋 ret=battle", 'ret = "battle"   # 已在战斗页' in src, True)
# 数量 9 -> 11 (2026-09-12): 新增两处"逃生后退回主城"的重导航调用点
# (主循环 等比赛界面超时/副本页 分支, 结算链 unknown 分支)
# 数量 11 -> 13 (2026-09-13): 新增两处"游戏失联自愈"的重导航调用点
# (_recover_from_game_loss 内部 / 主循环头 游戏存活预检分支)
# 数量 13 -> 14 (2026-09-13 下午): 新增"会话失效自愈"的重导航调用点 (_recover_from_session 内部)
# 数量 14 -> 16 (2026-09-14): 防挂机内层补 main_city 自愈 + pve_stage 逃生两处重导航调用点
# 数量 16 -> 17 (2026-09-22): 全民争霸"长时间不在对战列表"改为窗口内重导航回列表
check("全部调用点走重试包装", src.count("self._enter_pvp_hub_retry("), 17)  # 仅计调用点
check("无遗留直调(rank)", src.count('self._enter_pvp_hub("rank")'), 0)
check("无遗留直调(arena)", src.count('self._enter_pvp_hub("arena")'), 0)
check("战斗页分支置于三分支之前",
      src.index("_nav_page = \"battle\"") < src.index("_is_pvp_submenu(img)"), True)
# not_rank_hub 不再一击终止 (2026-09-12): 必须存在过渡缓冲 + 自愈重导航
check("not_rank_hub 有过渡缓冲复查", "页面过渡完成 (已就绪), 重新判定本局起点" in src, True)
# 2026-09-22 契约变更 (用户口径: "到我指定的排位时间了就要进入排位界面"):
# 自愈不再用固定次数上限, 改走统一闸门 _selfheal_ok -> 窗口内可持续, 窗口关/长时间无进展才停。
check("not_rank_hub 走统一自愈闸门", '_selfheal_ok("nav_rescues"' in src, True)
check("not_rank_hub 旧固定次数上限已移除",
      "self.nav_rescues > 3" not in src and "self.nav_rescues = getattr" not in src, True)
check("not_rank_hub 旧一击终止已移除",
      'self.log("warn", "当前不在荣耀排位赛主页 (未见顶部标题+主页独有元素), 任务终止")' in src,
      False)
# 五族自愈全部改走闸门 (nav×2 / game×1 / session×1 / main_city×2 / match×1)
check("自愈闸门覆盖五族 7 个调用点", src.count("_selfheal_ok("), 7 + 1)  # +1 = 方法定义

# 防挂机内层 ret 处理完整性 (2026-09-14 11:04 实锤): 弹窗悬停期间匹配倒计时归零,
# 即使答对也被踢回主城; 内层若不认 main_city 会落到兜底 ret != "battle" 冤枉终止。
check("防挂机内层处理 main_city (自愈重导航)", "防挂机通过但匹配已失效" in src, True)
check("防挂机内层 main_city 走同一闸门族",
      src.count('_selfheal_ok("main_city_rescues"'), 2)  # 外层 + 防挂机内层
check("防挂机内层处理 pve_stage (返回键逃生)", "防挂机等待期间落在 PvE 副本关卡页" in src, True)
check("防挂机兜底日志带 ret 值 (可观测)",
      "防挂机处理后仍未等到比赛界面 (未见跳过, ret=" in src, True)
# 战胜计数后清各族自愈计数 (连续语义) + 窗口内进度重置
check("打完一局清空各族自愈计数",
      "self.match_rescues = 0" in src and "self.session_rescues = 0" in src
      and "self.main_city_rescues = 0" in src, True)
check("进入战斗/结算链时重置窗口内无进展计时", "self._selfheal_progress()" in src, True)
# 全民争霸: 长时间不在列表 -> 窗口内重导航回对战列表 (而不是收工)
check("arena 不在列表时窗口内重导航", 'if not self._selfheal_ok("nav_rescues", "长时间不在对战列表页")' in src, True)

# ------------------------------------------------ D. 战斗页直连导航 (2026-09-12 P0 根因修复)
print("\n=== D. 战斗页直连导航 (P0 根因修复) ===")


def make_battle_nav(has_skip, has_x2=True, ocr_hit=False):
    """构造: 页面判定三函数全 False (即原实现必落 else 的场景).
    has_skip/has_x2 = 双锚点模板是否命中; ocr_hit = OCR 兜底是否读到"跳过"/"X2"."""
    b = object.__new__(Battler)
    b.errors = 0
    b._nav_page = None
    b.nav_rescues = 0
    b._nav_back_tried = False
    b.taps, b.logs, b.anomalies = [], [], []
    b.stop_evt = threading.Event()
    b._shot_img = lambda: IMG
    b._find_skip_btn = lambda img=None: (
        {"cx": 1497, "cy": 800, "score": 0.97} if has_skip else None)
    b._find_btn_tpl = lambda img, fname, roi, min_score=0.0: (
        {"cx": 1358, "cy": 822, "score": 1.0}
        if (fname.startswith("x2") and has_x2) else None)
    b._is_rank_hub = lambda img, fast=False: False
    b._is_pvp_submenu = lambda img: False
    b._is_city = lambda img: False
    b._is_in_main_city = lambda img: False   # 2026-09-13: 主城分支判据统一后必须显式 mock
    b._is_on_list = lambda img: False
    b._find_hint = lambda img, hints, roi=None, scale=1.0: (
        {"cx": 1497, "cy": 830} if ocr_hit else None)
    b._classify_page = lambda img=None: "unknown"
    b._tap = lambda x, y: b.taps.append((x, y))
    b._press_back = lambda: True
    b._sleep_stop = lambda s: None
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._save_anomaly = lambda tag: b.anomalies.append(tag)
    return b


b = make_battle_nav(True)
check("战斗页导航(rank) -> 直接成功", b._enter_pvp_hub("rank"), True)
check("  落点记为 battle", b._nav_page, "battle")
check("  零点击 (不打断本局)", b.taps, [])
check("  未存异常图", b.anomalies, [])
check("  日志说明接管", any("战斗页" in m for _, m in b.logs), True)

b = make_battle_nav(True)
check("战斗页导航(arena) -> 同样成功", b._enter_pvp_hub("arena"), True)
check("  arena 落点 battle", b._nav_page, "battle")

# 关键防误判: 擂台页"创建擂台"按钮会让 skip 模板命中(实测 0.718>0.65), 但无 X2
b = make_battle_nav(True, has_x2=False)
check("仅跳过模板命中(擂台页) -> 不判战斗页", b._is_in_battle(IMG), False)
check("  该页面导航仍判失败", b._enter_pvp_hub("rank"), False)

# 真机复现场景: 模板分数 0.632/0.650 < 0.65 阈值 -> 模板漏判, OCR 读到"跳过"
b = make_battle_nav(False, has_x2=False, ocr_hit=True)
check("模板漏判+OCR命中 -> 仍判为战斗页", b._is_in_battle(IMG), True)
check("  fast=True 不走 OCR -> 漏判(故热路径外禁用)", b._is_in_battle(IMG, fast=True), False)
check("  该场景导航仍成功", b._enter_pvp_hub("rank"), True)
check("  落点 battle", b._nav_page, "battle")

b = make_battle_nav(False, has_x2=False)
check("非战斗页 + 三分支全落空 -> 仍判失败", b._enter_pvp_hub("rank"), False)
check("  失败存 unknown_screen", "unknown_screen" in b.anomalies, True)
check("  日志带分类器结论", any("分类器判定=unknown" in m for _, m in b.logs), True)

# 未登记页面逃生 (2026-09-12 → 09-12 升级为"连按返回键"通用逃生)
b = make_battle_nav(False, has_x2=False)
backs = []
b._press_back = lambda: backs.append(1)
check("未登记页面 -> 逃生后仍失败(非静默终止)", b._enter_pvp_hub("rank"), False)
check("  返回键连按至上限(4 次)后放弃", len(backs), 4)
check("  逃生失败留图 (escape_failed_nav)", "escape_failed_nav" in b.anomalies, True)
check("  日志说明按了返回键", any("返回键" in m for _, m in b.logs), True)

# ------------------------------------------- E. 结算链原地接管 (2026-09-12, 消除冤枉终止)
print("\n=== E. 结算链原地接管 ===")
from sj_bot import rank_layout as rk   # noqa: E402


def make_settle_detect(reward=False, end=False, back=False, end_tpl=False,
                       tpl_state=None):
    """tpl_state: None=模板分落**中间区间** (交回 OCR 判定, 用于验证 OCR 兜底与顺序);
                 "hit"=>=HI 直接命中; "miss"=<=LO 直接否决 (P2 提速分支)。"""
    b = object.__new__(Battler)
    def find_hint(img, hints, roi=None, scale=1.0):
        if reward and roi == rk.REWARD_ROI:
            return {"cx": 0, "cy": 0}
        if end and roi == rk.END_ROI:
            return {"cx": 0, "cy": 0}
        if back and roi == rk.BACK_ROI:
            return {"cx": 0, "cy": 0}
        return None
    b._find_hint = find_hint
    b._ocr_calls = []          # 记录 OCR 是否被调用 (门控命中/否决必须为 0)

    def find_hint_counted(img, hints, roi=None, scale=1.0):
        b._ocr_calls.append(roi)
        return find_hint(img, hints, roi=roi, scale=scale)
    b._find_hint = find_hint_counted
    b._find_btn_tpl = lambda img, fname, roi, min_score=0.0: (
        {"cx": 0, "cy": 0} if (end_tpl and fname.startswith("end")) else None)

    def tpl_score(img, fname, roi):
        # P2 后 _detect_settle_stage 走 _hint_or_tpl -> _tpl_score, 必须一起注入,
        # 否则门控会去读磁盘模板并在真实帧上得到"低分" -> 直接否决, 绕过 OCR。
        # 2026-09-13 晚 reward 也入门控, mock 按真实帧实测行为建模:
        #   - 宝箱帧 (reward=True) reward 文案恒高分; 非宝箱帧恒低分
        #     (实测真 end 页上仅 0.352, ≤ LO) ⇒ 两个方向都不跑 OCR;
        #   - end/back 按 tpl_state 三档 (hit / miss / 中间)。
        if fname.startswith("reward"):
            return rk.REWARD_TPL_HI + 0.1 if reward else rk.REWARD_TPL_LO - 0.1
        if fname.startswith("end"):
            if end or end_tpl or tpl_state == "hit":
                return rk.END_TPL_HI + 0.1
            if tpl_state == "miss":
                return rk.END_TPL_LO - 0.1
            return (rk.END_TPL_LO + rk.END_TPL_HI) / 2
        if back or tpl_state == "hit":
            return rk.BACK_TPL_HI + 0.1
        if tpl_state == "miss":
            return rk.BACK_TPL_LO - 0.1
        return (rk.BACK_TPL_LO + rk.BACK_TPL_HI) / 2
    b._tpl_score = tpl_score
    return b


check("宝箱页 -> 'reward'", make_settle_detect(reward=True)._detect_settle_stage(IMG), "reward")
check("比赛结束页 -> 'end'", make_settle_detect(end=True)._detect_settle_stage(IMG), "end")
check("比赛结束页(仅模板命中) -> 'end'",
      make_settle_detect(end_tpl=True)._detect_settle_stage(IMG), "end")
check("返回大厅页 -> 'back'", make_settle_detect(back=True)._detect_settle_stage(IMG), "back")
check("非结算页 -> None", make_settle_detect()._detect_settle_stage(IMG), None)
check("宝箱优先于返回大厅", make_settle_detect(reward=True, back=True)._detect_settle_stage(IMG),
      "reward")
# ---- P2 门控行为 (2026-09-13): 高分命中 / 低分否决 都必须**跳过 OCR** ----
_b_hit = make_settle_detect(tpl_state="hit")
check("P2: 模板高分 -> 判 end 且不跑 OCR",
      (_b_hit._detect_settle_stage(IMG), len(_b_hit._ocr_calls)), ("end", 0))
# 注: reward 也入门控后 (2026-09-13 晚), 宝箱检查在非宝箱帧上同样 0 OCR
# (实测真 end 页上 reward 文案模板仅 0.352 ≤ LO) —— 结算链检测全程 0 OCR。
_b_miss = make_settle_detect(tpl_state="miss")
check("P2: 模板低分 -> 非结算页且 end/back 不跑 OCR",
      (_b_miss._detect_settle_stage(IMG), len(_b_miss._ocr_calls)), (None, 0))

check("主循环会检测结算链起点",
      "None if fast_hub else self._detect_settle_stage(img)" in src, True)
check("结算链起点跳过点匹配", 'if self._pre_settle_stage:\n                self.log("info", f"已在结算链' in src, True)
check("结算链起点跳过点跳过", "if self._pre_settle_stage is None:" in src, True)
check("结算流程入口 stage 参数化", 'stage = self._pre_settle_stage or "reward"' in src, True)
check("超时自愈接管结算链 (不再 no_battle_ui)",
      'elif page in ("reward", "end", "back"):' in src, True)

# ------------------------------------------- F. 通用逃生例程 (2026-09-12 用户需求)
print("\n=== F. 通用返回键逃生 (_escape_unknown_page) ===")


def make_escape(in_battle=False, pages=("unknown",), stop=False):
    b = object.__new__(Battler)
    b.stop_evt = threading.Event()
    if stop:
        b.stop_evt.set()
    b.logs, b.anomalies, b.backs = [], [], []
    b._is_in_battle = lambda img=None, fast=False: in_battle
    b._press_back = lambda: b.backs.append(1)
    b._sleep_stop = lambda s: None
    seq = list(pages)

    def cp(img=None):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    b._classify_page = cp
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._save_anomaly = lambda tag: b.anomalies.append(tag)
    return b


b = make_escape(in_battle=True)
check("已在战斗页 -> 不按返回键 (防误触退出确认)", b._escape_unknown_page("t"), "battle")
check("  零次按键", len(b.backs), 0)

b = make_escape(pages=("main_city",))
check("首次即退回主城 -> 返回 'main_city'", b._escape_unknown_page("t"), "main_city")
check("  只按 1 次", len(b.backs), 1)
check("  未存失败图", b.anomalies, [])

b = make_escape(pages=("unknown", "unknown", "hub"))
check("第 3 次才退回主页 -> 返回 'hub'", b._escape_unknown_page("t"), "hub")
check("  按了 3 次", len(b.backs), 3)

b = make_escape(pages=("unknown",))
check("始终无法识别 -> 返回 None", b._escape_unknown_page("t"), None)
check("  按满 tries=4 次后放弃", len(b.backs), 4)
check("  留逃生失败图", b.anomalies, ["escape_failed_t"])
check("  有失败日志", any("逃生失败" in m for _, m in b.logs), True)

b = make_escape(stop=True)
check("已请求停止 -> 立即返回 None", b._escape_unknown_page("t"), None)
check("  零次按键", len(b.backs), 0)

b = make_escape(pages=("unknown",), )
check("自定义 tries=2 生效", b._escape_unknown_page("t", tries=2), None)
check("  按键 2 次", len(b.backs), 2)

# ------------------------------------------- G. PvE 副本关卡页 (地下城章节图)
print("\n=== G. PvE 副本关卡页识别 + 逃生接线 ===")


def make_pve(hit_rois=()):
    b = object.__new__(Battler)
    b._find_hint = lambda img, hints, roi=None, scale=1.0: (
        {"cx": 0, "cy": 0} if roi in hit_rois else None)
    return b


check("标题 ROI 命中 -> 判为副本页", make_pve({rk.PVE_TITLE_ROI})._is_pve_stage(IMG), True)
check("星数 ROI 命中 -> 判为副本页", make_pve({rk.PVE_STAR_ROI})._is_pve_stage(IMG), True)
check("两 ROI 都不命中 -> 非副本页", make_pve()._is_pve_stage(IMG), False)
check("副本页锚点不含主城建筑名 (防误判)",
      any(h in rk.PVE_STAGE_HINTS for h in ("地下城", "深渊迷宫", "藏宝之地")), False)


def make_classify(**kw):
    b = object.__new__(Battler)
    b._find_hint = lambda img, hints, roi=None, scale=1.0: None
    b._find_btn_tpl = lambda img, fname, roi, min_score=0.0: None
    b._is_in_battle = lambda img=None, fast=False: kw.get("battle", False)
    b._is_rank_hub = lambda img=None, fast=False: kw.get("hub", False)
    b._is_in_main_city = lambda img: kw.get("city", False)
    b._is_pve_stage = lambda img=None: kw.get("pve", False)
    return b


check("_classify_page: 副本页 -> 'pve_stage'",
      make_classify(pve=True)._classify_page(IMG), "pve_stage")
check("_classify_page: 主城优先于副本页 (副本判据在最后)",
      make_classify(city=True, pve=True)._classify_page(IMG), "main_city")
check("_classify_page: 战斗页优先于副本页",
      make_classify(battle=True, pve=True)._classify_page(IMG), "battle")
check("_classify_page: 主页优先于副本页",
      make_classify(hub=True, pve=True)._classify_page(IMG), "hub")

check("主循环 pve_stage 分支存在", 'elif page == "pve_stage":' in src, True)
check("主循环 pve 分支走逃生", 'self._escape_unknown_page("pve_stage")' in src, True)
check("主循环 unknown 走逃生 (不再直接终止)",
      'landed = self._escape_unknown_page("match_timeout")' in src, True)
check("结算链 unknown 走逃生",
      'landed = self._escape_unknown_page(f"settle_{stage}")' in src, True)
check("导航 else 走逃生", 'self._escape_unknown_page("nav")' in src, True)
check("等比赛界面 20s 提前上报副本页 (不再干等 200s)", "f.pve_checked" in src, True)
check("逃生例程显式跳过战斗页",
      'if self._is_in_battle():\n            return "battle"' in src, True)
check("匹配按钮盲点回退加了主页确认",
      "且当前不在排位主页 -> 不盲点坐标" in src, True)

# ------------------------------------------- H. 稳态快车道 (2026-09-13 提速)
print("\n=== H. 稳态快车道 _try_fast_hub (2026-09-13 提速) ===")


def make_fast(hit_match=None, confident=True, glory=False):
    """hit_match=None 表示匹配按钮 OCR 未命中."""
    b = object.__new__(Battler)
    b._at_hub_confident = confident
    b.logs = []
    b.calls = []
    b.log = lambda lv, msg: b.logs.append((lv, msg))

    def fake_find(img, hints, roi=None, scale=1.0):
        b.calls.append(tuple(hints))
        return {"cx": 633, "cy": 500, "match": hit_match} if hit_match else None
    b._find_hint = fake_find
    return b


b = make_fast(hit_match="单人匹配")
check("标志为真 + 按钮命中 -> 走快车道", b._try_fast_hub(IMG, False), True)
check("  标志被消费 (不重复试)", b._at_hub_confident, False)
check("  只做 1 次 OCR", len(b.calls), 1)
check("  验证提示词 = 单人匹配/取消匹配 (不含组队匹配)",
      b.calls[0], ("单人匹配", "取消匹配"))
check("  有快车道日志", any("稳态快车道" in m for _, m in b.logs), True)

b = make_fast(hit_match="取消匹配")
check("按钮为'取消匹配'态也判在主页 -> 走快车道", b._try_fast_hub(IMG, False), True)

b = make_fast(hit_match=None)
check("按钮 OCR 未命中 -> 不走快车道 (退回完整判定链)", b._try_fast_hub(IMG, False), False)
check("  标志同样被消费", b._at_hub_confident, False)

b = make_fast(hit_match="单人匹配", confident=False)
check("标志为假 -> 不走快车道", b._try_fast_hub(IMG, False), False)
check("  零次 OCR (无额外开销)", len(b.calls), 0)

b = make_fast(hit_match="单人匹配", glory=True)
check("荣耀时刻 -> 不走快车道", b._try_fast_hub(IMG, True), False)
check("  荣耀下不产生 OCR", len(b.calls), 0)

# 静态断言: 快车道接线正确
check("快车道在循环头被调用", "fast_hub = self._try_fast_hub(img, is_glory)" in src, True)
check("快车道跳过结算链检测",
      "None if fast_hub else self._detect_settle_stage(img)" in src, True)
check("快车道跳过 30 次上限预检",
      "if not fast_hub and self._check_daily_limit(img):" in src, True)
check("快车道分支在战斗页判定之前",
      src.index("elif fast_hub:") < src.index("elif self._is_in_battle(img):"), True)
check("结算链正常完成后置 _at_hub_confident",
      "self._at_hub_confident = True" in src, True)
check("快车道不启用荣耀分支",
      "if not self._at_hub_confident:" in src and "if is_glory:" in
      src[src.index("def _try_fast_hub"):src.index("def _run_rank")], True)

# --------------------------------- I. 游戏存活探测 + 失联自愈 (2026-09-13 根因修复)
print("\n=== I. 游戏存活探测 / 失联自愈 (2026-09-13) ===")
GAME = "com.holyblade.sjlm.ganxt"
LAUNCHER = "app.lawnchair"


def make_health(fg, pid):
    """fg = _foreground_pkg 返回值 (None=查询失败); pid = _game_pid 返回值."""
    b = object.__new__(Battler)
    b.cfg = type("C", (), {"game_pkg": GAME})()
    b.logs = []
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._foreground_pkg = lambda: fg
    b._game_pid = lambda: pid
    return b


b = make_health(GAME, "1234")
check("前台就是游戏 -> ok", b._check_game_health(), "ok")
b = make_health(GAME, "")
check("前台是游戏但 pidof 为空 -> 仍判 ok (前台为准)", b._check_game_health(), "ok")
b = make_health(LAUNCHER, "1234")
check("前台非游戏但进程还在 -> background", b._check_game_health(), "background")
b = make_health(LAUNCHER, "")
check("前台非游戏且进程没了 -> gone", b._check_game_health(), "gone")
b = make_health(None, "")
check("前台查询失败 -> unknown (不误判为死)", b._check_game_health(), "unknown")
b = make_health(LAUNCHER, None)
check("前景非游戏但 pidof 查询失败 -> unknown (不误判为死)", b._check_game_health(), "unknown")


def make_ensure(fg_seq, launch_ok=True):
    """fg_seq: 每次 _foreground_pkg 调用依序弹出的前台包名 (末值重复使用)."""
    b = make_health(fg_seq[0], "")
    b.seq = list(fg_seq)
    b.launched = []
    b.stop_evt = threading.Event()
    b._sleep_stop = lambda s: None

    def fg():
        return b.seq.pop(0) if len(b.seq) > 1 else b.seq[0]
    b._foreground_pkg = fg
    b._launch_game = lambda: (b.launched.append(1), launch_ok)[1]
    return b


b = make_ensure([GAME])
check("已在前台 -> 就绪且不拉起", b._ensure_game_foreground("t"), True)
check("  未调用拉起", b.launched, [])

b = make_ensure([LAUNCHER, GAME])
check("被切到桌面 -> 拉起后回到游戏 = 就绪", b._ensure_game_foreground("t"), True)
check("  确实拉起了", len(b.launched), 1)

b = make_ensure([LAUNCHER], launch_ok=False)
check("拉起失败 -> False", b._ensure_game_foreground("t"), False)

b = make_ensure([None])
check("状态未知 -> 保守放行 (不下结论/不拉起)", b._ensure_game_foreground("t"), True)
check("  未调用拉起", b.launched, [])


def make_recover(ensure_ok=True, nav_ok=True, rescues=0):
    b = object.__new__(Battler)
    b.game_rescues = rescues
    b.logs = []
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b.anomalies = []
    b._save_anomaly = lambda tag: b.anomalies.append(tag)
    b._sleep_stop = lambda s: None
    b._ensure_game_foreground = lambda tag="": ensure_ok
    b._enter_pvp_hub_retry = lambda mode, tries=3: nav_ok
    return b


b = make_recover()
check("失联自愈: 拉起+导航成功 -> recovered", b._recover_from_game_loss(), "recovered")
check("  计数 +1", b.game_rescues, 1)
check("  存了现场图", b.anomalies, ["game_gone"])
b = make_recover(ensure_ok=False)
check("拉起失败 -> stop", b._recover_from_game_loss(), "stop")
b = make_recover(nav_ok=False)
check("导航失败 -> nav_fail", b._recover_from_game_loss(), "nav_fail")
b = make_recover(rescues=3)
check("自愈次数已达上限 -> stop (防无限重试)", b._recover_from_game_loss(), "stop")

# 静态断言: 接线正确
check("等比赛界面有游戏失联快速失败",
      "f.health_checked" in src and '"game_gone"' in src, True)
check("game_gone 在 _wait_battle_ui 返回白名单",
      '"pve_stage", "game_gone", "session")' in src, True)
check("主循环头有游戏存活预检", 'if self._game_pid() == "":' in src, True)
check("主循环 game_gone 走自愈", "_recover_from_game_loss()" in src, True)
check("逃生例程先查游戏存活",
      'if self._check_game_health() in ("gone", "background"):' in src, True)
check("_recover_from_game_loss 会重导航", "self._enter_pvp_hub_retry" in
      src[src.index("def _recover_from_game_loss"):src.index("def _press_back")], True)
check("游戏包名只在 config 里定义 (battler 无硬编码)",
      '"com.holyblade' not in src and "com.holyblade" not in
      open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "sj_bot", "config.py"), encoding="utf-8").read().replace(
          'os.environ.get("SJ_GAME_PKG") or "com.holyblade.sjlm.ganxt"', ""), True)

# OCR 缓存 (2026-09-13 提速): 同一帧同一 ROI 只真正 OCR 一次
check("_ocr_texts 有同帧缓存", "_ocr_cache_frame" in src, True)
check("_shot_img 换帧时清缓存", "self._ocr_cache = {}" in src, True)
check("缓存用身份比较 (非 id, 防旧帧串味)",
      "getattr(self, \"_ocr_cache_frame\", None) is img" in src, True)
check("导航确认在主页时置 _at_hub_confident",
      src.count("self._at_hub_confident = True"), 3)

# ------------------------- J. 会话失效弹窗 / 登录页 自愈 (2026-09-13 根因修复)
print("\n=== J. 会话失效 / 登录页 自愈 (2026-09-13) ===")
import sj_bot.battler as _B


class _FakeTime:
    """快进时钟: 每次 time() +10s, sleep 无操作 -> 45s 轮询循环瞬间跑完."""
    def __init__(self):
        self.t = 0.0

    def time(self):
        self.t += 10.0
        return self.t

    def sleep(self, _s):
        pass


def make_session(expired=True, login=False, nav_ok=True, main_city=True, rescues=0):
    b = object.__new__(Battler)
    b.session_rescues = rescues
    b.kind = "r"
    b.logs, b.taps = [], []
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._save_anomaly = lambda tag: None
    b._shot_img = lambda: IMG
    b._sleep_stop = lambda s: None
    b._press_back = lambda: True
    b._tap = lambda x, y: b.taps.append((x, y))
    b._is_session_expired = lambda img=None: expired
    b._is_login_screen = lambda img=None: login
    b._is_in_main_city = lambda img: main_city
    b._is_rank_hub = lambda img, fast=False: False
    b._enter_pvp_hub_retry = lambda mode, tries=3: nav_ok
    b.stop_evt = threading.Event()
    return b


_real_time = _B.time
try:
    _B.time = _FakeTime()
    b = make_session(expired=True, login=False)
    check("会话失效弹窗: 点确认+重导航 -> recovered", b._recover_from_session("t"), "recovered")
    check("  点了确认按钮坐标", b.taps[0], (800, 578))
    check("  计数 +1", b.session_rescues, 1)

    b = make_session(expired=False, login=True)
    check("登录页: 点开始+重导航 -> recovered", b._recover_from_session("t"), "recovered")
    check("  点了开始按钮坐标", b.taps[0], (775, 523))

    b = make_session(expired=True, login=True)
    b._recover_from_session("t")
    check("弹窗+登录页: 先确认后开始", b.taps, [(800, 578), (775, 523)])

    b = make_session(expired=True, nav_ok=False)
    check("重导航失败 -> nav_fail", b._recover_from_session("t"), "nav_fail")
    b = make_session(expired=True, rescues=3)
    check("自愈次数超限 -> stop", b._recover_from_session("t"), "stop")
    b = make_session(expired=True, main_city=False)
    _B.time = _FakeTime()
    check("45s 未回主城也照常尝试导航 -> recovered", b._recover_from_session("t"), "recovered")
finally:
    _B.time = _real_time

# _classify_page 的优先级: 会话弹窗必须最先判 (它是覆盖战斗页的模态遮罩)
def make_cls(session=False, login=False, battle=True):
    b = object.__new__(Battler)
    b._shot_img = lambda: IMG
    b._is_session_expired = lambda img=None: session
    b._is_login_screen = lambda img=None: login
    b._find_hint = lambda img, hints, roi=None, scale=1.0, min_score=0.0: None
    b._find_btn_tpl = lambda img, name, roi: False
    b._is_in_battle = lambda img, fast=False: battle
    b._is_rank_hub = lambda img, fast=False: False
    b._is_in_main_city = lambda img: False
    b._is_pve_stage = lambda img=None: False
    return b


check("_classify_page: 会话弹窗盖在战斗页上 -> session (不是 battle)",
      make_cls(session=True).__class__ and make_cls(session=True)._classify_page(IMG), "session")
check("_classify_page: 登录页 -> login", make_cls(login=True, battle=False)._classify_page(IMG), "login")
check("_classify_page: 无弹窗时战斗页仍 -> battle", make_cls()._classify_page(IMG), "battle")

# 静态断言: 接线
check("rank_layout 有 SESSION_HINTS/LOGIN_HINTS",
      "SESSION_HINTS" in open(os.path.join(os.path.dirname(os.path.dirname(
          os.path.abspath(__file__))), "sj_bot", "rank_layout.py"), encoding="utf-8").read(), True)
check("等比赛界面有会话失效一次性探查", "f.session_checked" in src, True)
check("session 在 _wait_battle_ui 返回白名单", '"game_gone", "session")' in src, True)
check("结算自愈有 session 分支", 'if page in ("session", "login"):' in src, True)
check("结算自愈恢复后回主循环 (__retry__)", 'flow_exit == "__retry__"' in src, True)
check("匹配超时自愈有 session 分支", 'elif page in ("session", "login"):' in src, True)
check("逃生例程先查会话失效",
      "self._is_session_expired() or self._is_login_screen()" in src, True)
check("主循环处理 session 返回码", 'if ret == "session":' in src, True)
check("导航入口先处理会话失效/登录页", 'self._session_nav_guard' in src, True)
check("  且放在战斗页判定之前",
      src.index("self._session_nav_guard") < src.index("if self._is_in_battle(img):\n            self._nav_page"), True)
check("逃生拉起游戏后停在登录页会补登录", 'if page in ("login", "session"):' in src, True)

# ------------------------- K. 判据条带化提速 (2026-09-13 下午 P1)
print("\n=== K. 主页/主城判据条带化 (2026-09-13 P1) ===")
_rk_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sj_bot", "rank_layout.py"), encoding="utf-8").read()
_f = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "sj_bot", "battler.py")
check("rank_layout 定义 HUB_STRIP_ROI", "HUB_STRIP_ROI = (0, 270, 1600, 670)" in _rk_src, True)
check("rank_layout 定义主城 tab 快路径常量",
      "MAIN_CITY_TAB_ROI" in _rk_src and "MAIN_CITY_TAB_MIN_HITS" in _rk_src, True)
check("_is_rank_hub 非快路径改用条带 (不再全图)",
      "rk.HUB_UNIQUE_HINTS, roi=rk.HUB_STRIP_ROI" in src, True)
check("_is_rank_hub 旧全图调用已移除",
      "self._find_hint(img, rk.HUB_UNIQUE_HINTS, scale=1.0) is not None" in src, False)
check("_is_in_main_city 先走 tab 快路径", "roi=rk.MAIN_CITY_TAB_ROI" in src, True)
check("_is_in_main_city 快路径后仍保留全图兜底",
      "for it in self._ocr_texts(img, scale=1.0):" in src, True)
check("条带等价性验证脚本存在",
      os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "scripts", "verify_hub_strip.py")), True)

# ------------------------- L. 扁条 ROI 血案防回归 (2026-09-13 下午, 动态断言)
print("\n=== L. 扁条 OCR ROI 守卫 (动态读常量) ===")
from sj_bot import rank_layout as _rk     # noqa: E402

_mc_h = _rk.MAIN_CITY_TAB_ROI[3] - _rk.MAIN_CITY_TAB_ROI[1]
check("MAIN_CITY_TAB_ROI 高度 >= 192 (90px 时恒返回 0 块)", _mc_h >= 192, True)
check("MAIN_CITY_TAB_MIN_HITS 取 3 (实测正 5/5, 负 0/5)", _rk.MAIN_CITY_TAB_MIN_HITS, 3)
check("_is_in_main_city tab 命中按**去重**计数",
      "tab_hits: set[str] = set()" in src, True)
_hub_h = _rk.HUB_STRIP_ROI[3] - _rk.HUB_STRIP_ROI[1]
check("HUB_STRIP_ROI 高度 >= 192", _hub_h >= 192, True)
check("条带覆盖主页独有元素纵向范围",
      _rk.HUB_STRIP_ROI[1] <= 270 and _rk.HUB_STRIP_ROI[3] >= 670, True)
# 已用真实正样本验过可检出的"允许偏薄"白名单 (见 verify_hub_strip.py 同名守卫)
_thin_ok = {"SESSION_TEXT_ROI", "PVE_TITLE_ROI", "PVE_STAR_ROI",
            "HUB_BTN_ROI", "BACK_ROI", "X2_ROI"}
_thin_bad = []
for _n in dir(_rk):
    if "ROI" not in _n or _n.startswith("_") or _n in _thin_ok:
        continue
    _v = getattr(_rk, _n)
    if (isinstance(_v, tuple) and len(_v) == 4
            and all(isinstance(x, int) for x in _v) and _v[3] - _v[1] < 192):
        _thin_bad.append((_n, _v[3] - _v[1]))
check("无未验证的薄 OCR ROI", _thin_bad, [])

# ------------------------- M. 可观测性 + "不许用 fast 提速" 防回归 (2026-09-13)
print("\n=== M. 导航可观测性与判据选型守卫 ===")
check("_classify_page 的 hub 判据不是 fast=True (匹配中会漏判)",
      "self._is_rank_hub(img)" in src, True)
check("  fast=True 仍只用于稳态快车道", "_is_rank_hub(img, fast=True)" in src, True)
check("导航轮次总耗时会写日志",
      '"本轮导航 ' in src or "[耗时] 本轮导航" in src, True)
check("落点判定耗时会写日志", '"[耗时] 落点判定' in src, True)
check("起步→点匹配秒数会写日志", "起步→点匹配" in src, True)
check("fast/条带等价性否决脚本存在",
      os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "scripts", "verify_hub_fast.py")), True)
check("分类耗时分解脚本存在",
      os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "scripts", "diag_classify_breakdown.py")), True)

# ------------------------- N. P2 三值模板门控 (2026-09-13 晚, 提速主体)
print("\n=== N. P2 三值模板门控 (end/back 跳过恒负 OCR) ===")
check("rank_layout 定义 end 门控阈值",
      hasattr(_rk, "END_TPL_HI") and hasattr(_rk, "END_TPL_LO"), True)
check("rank_layout 定义 back 门控阈值",
      hasattr(_rk, "BACK_TPL_HI") and hasattr(_rk, "BACK_TPL_LO"), True)
# 阈值序关系: 0 < LO < HI < 1 是门控成立的必要条件
for _k in ("END", "BACK"):
    _hi = getattr(_rk, f"{_k}_TPL_HI")
    _lo = getattr(_rk, f"{_k}_TPL_LO")
    check(f"{_k}: 0 < LO < HI < 1", 0.0 < _lo < _hi < 1.0, True)
# ⚠️ 安全约束: LO 必须**高于负样本实测上界**, 否则"稳态帧直接否决"这个提速分支不成立;
#    HI 必须**低于正样本实测下限**, 否则真结算页会落进"未命中"被静默漏判。
#    实测 (scripts/verify_tpl_gate.py, 灰度匹配 = 生产算法): end 负上界 0.461 / 正下界 0.844;
#    back 负上界 0.328 / 正下界 0.823。这里用真实帧动态复算, 模板/截图变了会立刻失败。
import cv2 as _cv2      # noqa: E402
import numpy as _np     # noqa: E402


def _gray(_p):
    _b = _np.fromfile(_p, dtype=_np.uint8)
    return _cv2.cvtColor(_cv2.imdecode(_b, _cv2.IMREAD_COLOR), _cv2.COLOR_BGR2GRAY)


def _score(_img, _tpl, _roi):
    _x0, _y0, _x1, _y1 = _roi
    _crop = _cv2.cvtColor(_img[_y0:_y1, _x0:_x1], _cv2.COLOR_BGR2GRAY)
    if _crop.shape[0] < _tpl.shape[0] or _crop.shape[1] < _tpl.shape[1]:
        return None
    return float(_cv2.matchTemplate(_crop, _tpl, _cv2.TM_CCOEFF_NORMED).max())


_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cap = os.path.join(_root, "captures")
_assets = os.path.join(_root, "assets")
_labelled = {"rank_4_end.png": "end", "anomaly_104531_no_end.png": "end",
             "rank_5_back.png": "back",
             "rank_hub_clean.png": "-", "rank_0_hub.png": "-", "rank_1_matched.png": "-",
             "anomaly_111325_no_end.png": "-", "rank_3_reward.png": "-",
             "rank_2_battle.png": "-", "_p0_baseline.png": "-"}
_gate_tbl = {"end": _gray(os.path.join(_assets, "end_btn.png")),
             "back": _gray(os.path.join(_assets, "back_btn.png"))}
_gate_roi = {"end": _rk.END_ROI, "back": _rk.BACK_ROI}
_pos_min = {"end": 1.0, "back": 1.0}
_neg_max = {"end": 0.0, "back": 0.0}
_frames_seen = 0
for _fn, _truth in _labelled.items():
    _p = os.path.join(_cap, _fn)
    if not os.path.exists(_p):
        continue
    _img = _cv2.imdecode(_np.fromfile(_p, dtype=_np.uint8), _cv2.IMREAD_COLOR)
    if _img is None or _img.shape[:2] != (900, 1600):
        continue
    _frames_seen += 1
    for _k in ("end", "back"):
        _s = _score(_img, _gate_tbl[_k], _gate_roi[_k])
        if _s is None:
            continue
        if _truth == _k:
            _pos_min[_k] = min(_pos_min[_k], _s)
        else:
            _neg_max[_k] = max(_neg_max[_k], _s)
check("真实帧样本可用 (>=9 帧)", _frames_seen >= 9, True)
check("end: LO 高于负样本上界 (稳态帧可直接否决)", _rk.END_TPL_LO > _neg_max["end"], True)
check("end: HI 低于正样本下限 (真 end 页不会被判未命中)",
      _rk.END_TPL_HI < _pos_min["end"], True)
check("back: LO 高于负样本上界", _rk.BACK_TPL_LO > _neg_max["back"], True)
check("back: HI 低于正样本下限", _rk.BACK_TPL_HI < _pos_min["back"], True)
# 调用点: end/back 一律走门控, 旧的 "模板 or OCR" 写法必须绝迹
check("_classify_page 的 end 走三值门控",
      'self._hint_or_tpl(img, "end_btn.png"' in src, True)
check("_classify_page 的 back 走三值门控",
      'self._hint_or_tpl(img, "back_btn.png"' in src, True)
check("_detect_settle_stage 的 end 走三值门控",
      src.count('self._hint_or_tpl(img, "end_btn.png"') >= 2, True)
check("_detect_settle_stage 的 back 走三值门控",
      src.count('self._hint_or_tpl(img, "back_btn.png"') >= 2, True)
check("旧 'end 模板 or OCR' 写法已移除",
      'self._find_btn_tpl(img, "end_btn.png", rk.END_ROI)' in src, False)
check("旧 'back 模板 or OCR' 写法已移除",
      'self._find_btn_tpl(img, "back_btn.png", rk.BACK_ROI)' in src, False)
# 门控自身语义: 三分支齐备 + 无法判断时**必须回退 OCR** (而不是当成未命中)
check("_hint_or_tpl 存在", "def _hint_or_tpl(" in src, True)
check("门控: 高分判命中", "if sc >= hi:" in src, True)
check("门控: 低分判未命中", "if sc <= lo:" in src, True)
check("门控: 中间/取不到分回退 OCR",
      "return bool(self._find_hint(img, hints, roi=roi, scale=1.0))" in src, True)
check("_tpl_score 有 NaN/常量图保护", "np.isfinite(mv)" in src, True)
check("门控等价性验证脚本存在",
      os.path.exists(os.path.join(_root, "scripts", "verify_tpl_gate.py")), True)

# ============ Section O: rank 入口动态定位 (P3, 2026-09-13) ============
# 依据: 丝带标题不可点, 可点区 = 标题中心 +87px; 模板正 0.869~1.000 / 负上界 0.481。
print("\n--- Section O: rank 入口动态定位 ---")
import cv2  # noqa: E402

check("assets/rank_entry_ribbon.png 存在",
      os.path.exists(os.path.join(_root, "assets", "rank_entry_ribbon.png")), True)
check("阈值常量齐备 (DY/ROI/TPL_MIN)",
      all(hasattr(rk, k) for k in ("RANK_ENTRY_DY", "RANK_ENTRY_ROI", "RANK_ENTRY_TPL_MIN")), True)
check("RANK_ENTRY_DY = 87 (实测 605-518)", rk.RANK_ENTRY_DY, 87)
check("RANK_ENTRY_ROI 在卡片行且避开主页同名标题 (y>=400)",
      rk.RANK_ENTRY_ROI[1] >= 400, True)

# 动态正负样本复算 (灰度 = 生产算法), 模板若被重裁, 这里会立刻暴露分布漂移
_tpl = cv2.imdecode(np.fromfile(os.path.join(_root, "assets", "rank_entry_ribbon.png"),
                                dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
def _entry_score(fn):
    p = os.path.join(_root, "captures", fn)
    if not os.path.exists(p):
        return None
    g = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    x0, y0, x1, y1 = rk.RANK_ENTRY_ROI
    r = cv2.matchTemplate(g[y0:y1, x0:x1], _tpl, cv2.TM_CCOEFF_NORMED)
    mv = float(r.max())
    return mv if np.isfinite(mv) else None

POS_FRAMES = ["_p3_submenu.png", "_p3_t0.png", "_p3_t1.png", "_p3_v1.png", "_p3_b0.png"]
NEG_FRAMES = ["_p3_s2.png", "_p3_b1.png", "_p3_u0.png", "_p3_v0.png",
              "rank_hub_clean2.png", "rank_1_matched.png",
              "anomaly_174041_cooldown_nav_fail.png"]
pos = [s for s in (_entry_score(f) for f in POS_FRAMES) if s is not None]
neg = [s for s in (_entry_score(f) for f in NEG_FRAMES) if s is not None]
check(f"正样本均有分 ({len(pos)}/{len(POS_FRAMES)} 帧在库)", len(pos) == len(POS_FRAMES), True)
check(f"负样本均有分 ({len(neg)}/{len(NEG_FRAMES)} 帧在库)", len(neg) == len(NEG_FRAMES), True)
check(f"正样本下界 {min(pos):.3f} >= TPL_MIN({rk.RANK_ENTRY_TPL_MIN})", min(pos) >= rk.RANK_ENTRY_TPL_MIN, True)
check(f"负样本上界 {max(neg):.3f} < TPL_MIN", max(neg) < rk.RANK_ENTRY_TPL_MIN, True)
check(f"阈值两侧留空隙 (负上界+0.1 <= MIN <= 正下界-0.1)",
      max(neg) + 0.1 <= rk.RANK_ENTRY_TPL_MIN <= min(pos) - 0.1, True)

# _tap_rank_entry 三级降级语义 (mock 注入, 记录点击点)
def _make_taper(tpl_hit, ocr_hit):
    b = object.__new__(Battler)
    b.log = lambda lv, m: None
    taps = []
    b._tap = lambda x, y: taps.append((x, y))
    b._find_btn_tpl = (lambda img, fname, roi, min_score=0.0:
                       {"cx": 615, "cy": 518, "score": 0.99} if tpl_hit else None)
    b._find_hint = (lambda img, hints, roi=None, scale=1.0:
                    {"cx": 614, "cy": 518} if ocr_hit else None)
    return b, taps

b, taps = _make_taper(True, False); b._tap_rank_entry(None)
check("模板命中 -> 点 (cx, cy+DY)", taps, [(615, 518 + rk.RANK_ENTRY_DY)])
b, taps = _make_taper(False, True); b._tap_rank_entry(None)
check("模板未命中 -> OCR 锚点 +DY (同为动态)", taps, [(614, 518 + rk.RANK_ENTRY_DY)])
b, taps = _make_taper(False, False); b._tap_rank_entry(None)
check("双失效 -> 回退固定热区 (可用性不降级)", taps, [tuple(rk.RANK_ENTRY_HOTSPOT)])

# 调用点接线: 两处 rank 分支都走 _tap_rank_entry; RANK_ENTRY_HOTSPOT 只允许出现在
# _tap_rank_entry 内部兜底分支 (恰好 1 处), 导航主流程不得再直接点硬编码热区
check("二级菜单分支走 _tap_rank_entry", "self._tap_rank_entry(img)" in src, True)
check("RANK_ENTRY_HOTSPOT 代码引用仅剩 _tap_rank_entry 兜底 1 处",
      src.count("= rk.RANK_ENTRY_HOTSPOT"), 1)
check("主城分支取新帧后走 _tap_rank_entry",
      "self._tap_rank_entry(img2)" in src, True)
check("主城分支点入口前重取帧 (旧 img 是主城帧)",
      "img2 = self._shot_img()" in src, True)

# ============ Section P: arena 对战列表判据 (2026-09-13 首跑翻车复盘) ============
# 旧判据 `蓝青矩形 and 子串'挑战'` 双向翻车: 主城「每日挑战1次竞技场」误判 True,
# 真实列表 (按钮 y 248~630 > 旧 ROI 下沿 400) 误判 False。新判据 = 短「挑战」块计数 >=2。
print("\n--- Section P: arena 对战列表判据 ---")
import sj_bot.battler as _bt  # noqa: E402

_src_b = io_open = None
_src_b = open(os.path.join(_root, "sj_bot", "battler.py"), encoding="utf-8").read()
_seg = _src_b[_src_b.find("def _is_on_list"):]
_seg = _seg[:_seg.find("\n    def ", 10)]

check("LIST_BTN_ROI 下沿放宽到 >= 700 (真实按钮 y 248~630)",
      _bt.LIST_BTN_ROI[3] >= 700, True)
check("_is_on_list 实现不含 find_button_rects (色块 mask 从必要条件中剔除)",
      "find_button_rects" not in _seg, True)
check("_is_on_list 用短块计数 (>=2 个独立「挑战」)",
      "n >= 2" in _seg, True)
check("_is_on_list 不再直接用子串 _find_hint('挑战') (长句误命中源)",
      '_find_hint(img, ("挑战",)' not in _seg, True)
_src_v = open(os.path.join(_root, "sj_bot", "vision.py"), encoding="utf-8").read()
check("扫描按钮 ROI 走 arena_layout.BTN_COLUMN_ROI (不再写死 380 下沿)",
      "roi=L.BTN_COLUMN_ROI" in _src_v, True)
import sj_bot.arena_layout as _al  # noqa: E402
check("battler.LIST_BTN_ROI 与 arena_layout.BTN_COLUMN_ROI 值一致 (两处改动要同步)",
      _bt.LIST_BTN_ROI == _al.BTN_COLUMN_ROI, True)
check("帧级验证脚本存在",
      os.path.exists(os.path.join(_root, "scripts", "verify_arena_list.py")), True)

# stale 拉黑 (2026-09-13 第五次翻车): 同一「挑战但点不动」行被反复选中 5 连阻断 ->
# 本任务内拉黑, _pick_target 必须向下选择
import sj_bot.vision as _vis  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

check("stale 时把对手记入本任务拉黑名单", "self._stale_names.add(name)" in _src_b, True)
check("_pick_target 含拉黑过滤", "opp.name in self._stale_names" in _src_b, True)
_b2 = object.__new__(Battler)
_b2.mode = "arena"
_b2.log = lambda lv, m: None
_b2._stale_names = {"45区 雪人夜樱花"}


class _FakeDB2:
    def weekly_win_points(self):
        return 0


class _FakeFlow2:
    def __init__(self, db):
        self.db = db

    def decide(self, opp, use_cap=True):
        return "battle"


_b2.flow = _FakeFlow2(_FakeDB2())
_saved_scan = _vis.scan_opponent_list
_vis.scan_opponent_list = lambda img: [
    _NS(name="45区 雪人夜樱花", btn=(1500, 125)),
    _NS(name="1区 可打的人", btn=(1500, 312)),
]
try:
    _t = _b2._pick_target(None)
finally:
    _vis.scan_opponent_list = _saved_scan
check("_pick_target 跳过拉黑行向下选择", _t, ("1区 可打的人", (1500, 312)))

# ============ Section Q: reward 门控 (P2 扩展, 2026-09-13 晚) ============
print("\n--- Section Q: reward 门控 ---")
check("REWARD_TPL_HI/LO 存在且 HI > LO",
      hasattr(rk, "REWARD_TPL_HI") and hasattr(rk, "REWARD_TPL_LO")
      and rk.REWARD_TPL_HI > rk.REWARD_TPL_LO, True)
check("assets/reward_pick_text.png 存在",
      os.path.exists(os.path.join(_root, "assets", "reward_pick_text.png")), True)
check("_classify_page 与 _detect_settle_stage 的 reward 判据均走门控 (共 2 处)",
      src.count('_hint_or_tpl(img, "reward_pick_text.png"'), 2)
check("reward 判据不再直接调 _find_hint(REWARD_HINTS) (恒负 OCR 已消)",
      "self._find_hint(img, rk.REWARD_HINTS, roi=rk.REWARD_ROI" not in src, True)

# ============ Section R: 游戏失联×导航/逃生 闭环 (2026-09-13 18:0x 荣耀 nav_failed) ============
print("\n--- Section R: 游戏失联×导航/逃生 闭环 ---")
# 实录: 主城按 back 弹退出确认框 -> 连按把游戏按退出到模拟器桌面 ->
# 逃生循环内不查健康 4 连白按 -> 重试 2/3、3/3 在桌面上盲跑 35s OCR -> nav_failed。
# 三个修复: ① 逃生循环内每次返回后复查游戏健康 ② 重试包装每轮尝试前健康检查
# ③ _enter_pvp_hub 主城分支判据与 _classify_page 统一 (_is_in_main_city)。

# R1. 逃生中途游戏被按退出 -> 改走拉起游戏
def make_escape_health(health_seq, relaunch_page="hub", ensure_ok=True):
    b = object.__new__(Battler)
    b.stop_evt = threading.Event()
    b.logs, b.anomalies, b.backs, b.launches = [], [], [], []
    b._is_in_battle = lambda img=None, fast=False: False
    b._press_back = lambda: b.backs.append(1)
    b._sleep_stop = lambda s: None
    b._classify_page = lambda img=None: "unknown"
    b._is_session_expired = lambda img=None: False
    b._is_login_screen = lambda img=None: False
    hs = list(health_seq)

    def h():
        v = hs.pop(0) if len(hs) > 1 else hs[0]
        return v
    b._check_game_health = h

    def ensure(tag=""):
        b.launches.append(tag)
        return ensure_ok
    b._ensure_game_foreground = ensure
    b._recover_from_session = lambda tag="": "recovered" if relaunch_page == "hub" else "fail"
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._save_anomaly = lambda tag: b.anomalies.append(tag)
    # _classify_page: 拉起成功后才能"回到"已知页 (relaunch_page), 其余一律 unknown
    def cp(img=None):
        return relaunch_page if (ensure_ok and b.launches) else "unknown"
    b._classify_page = cp
    return b


b = make_escape_health(["ok", "ok", "gone"], relaunch_page="hub")
check("R1 逃生第2次返回后游戏gone -> 改拉起并回hub",
      b._escape_unknown_page("t"), "hub")
check("R1 只按了 2 次返回键就止损", len(b.backs), 2)
check("R1 拉起游戏被调用 1 次", len(b.launches), 1)
check("R1 未留逃生失败图", b.anomalies, [])

b = make_escape_health(["ok", "gone"], relaunch_page="unknown")
check("R2 拉起后仍不认 -> 返回 None 收口", b._escape_unknown_page("t"), None)
check("R2 留逃生失败图", b.anomalies, ["escape_failed_t"])
check("R2 只按 1 次返回键 (不在桌面空转)", len(b.backs), 1)

b = make_escape_health(["ok", "ok", "gone"], ensure_ok=False)
check("R3 拉起失败 -> 直接收口 None", b._escape_unknown_page("t"), None)
check("R3 未留图前按了 2 次键", len(b.backs), 2)

# R4. 导航重试包装: 每轮尝试前健康检查
def make_retry_health(health_seq):
    b = object.__new__(Battler)
    b.stop_evt = threading.Event()
    b.logs = []
    b.sleeps = []
    b.calls = []
    b.launches = []
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._sleep_stop = lambda s: b.sleeps.append(s)
    hs = list(health_seq)

    def h():
        return hs.pop(0) if len(hs) > 1 else hs[0]
    b._check_game_health = h

    def ensure(tag=""):
        b.launches.append(tag)
        return True
    b._ensure_game_foreground = ensure

    def fake(mode):
        b.calls.append(mode)
        i = len(b.calls) - 1
        return i >= 1   # 第 1 次失败, 第 2 次成功
    b._enter_pvp_hub = fake
    return b


b = make_retry_health(["gone", "ok", "ok", "ok"])
check("R4 第1轮尝试前游戏gone -> 先拉起再导航", b._enter_pvp_hub_retry("rank"), True)
check("R4 拉起调用 1 次 (第2轮已 ok 不再拉)", len(b.launches), 1)
check("R4 导航尝试 2 次", len(b.calls), 2)

# R5. 静态断言: 三处修复都在位
check("R5 逃生循环内含健康复查 (每次返回后)",
      "if self._check_game_health() in (\"gone\", \"background\"):" in src
      and src.count("_escape_relaunch_game(") >= 3, True)   # 定义+入口+循环内
check("R5 重试包装含每轮尝试前健康检查",
      "导航第 {i} 次尝试前游戏不在前台" in src, True)
check("R5 主城分支判据与分类器统一 (_is_in_main_city)",
      "elif self._is_city(img) or self._is_in_main_city(img):" in src, True)

# R6. 主城锚点丢失 -> 重截一帧; 仍无 -> 留图收口, **绝不按返回键**
def make_city_nav(anchor_frames, main_city=True):
    """anchor_frames: 每次 _find_hint(王者之巅) 的返回序列; None 帧 = 截图失败."""
    b = object.__new__(Battler)
    b.errors = 0
    b._nav_page = None
    b._nav_back_tried = False
    b.taps, b.logs, b.anomalies, b.backs = [], [], [], []
    b.stop_evt = threading.Event()
    shots = [IMG] * (2 if anchor_frames else 1)
    b._shot_img = lambda: shots.pop(0) if len(shots) > 1 else shots[0]
    b._is_in_battle = lambda img=None, fast=False: False
    b._is_rank_hub = lambda img, fast=False: False
    b._is_pvp_submenu = lambda img: False
    b._is_city = lambda img: False
    b._is_in_main_city = lambda img: main_city
    b._is_on_list = lambda img: False
    b._is_session_expired = lambda img=None: False   # 防 _find_hint mock 序列被消费
    b._is_login_screen = lambda img=None: False
    ah = list(anchor_frames)

    def fh(img, hints, roi=None, scale=1.0):
        return ah.pop(0) if len(ah) > 1 else (ah[0] if ah else None)
    b._find_hint = fh
    b._wait_hint = lambda *a, **k: {"cx": 1, "cy": 1}
    b._tap_rank_entry = lambda img: b.taps.append(("rank_entry", img))
    b._classify_page = lambda img=None: "hub"
    b._tap = lambda x, y: b.taps.append((x, y))
    b._press_back = lambda: b.backs.append(1)
    b._sleep_stop = lambda s: None
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._save_anomaly = lambda tag: b.anomalies.append(tag)
    return b


b = make_city_nav([None, {"cx": 613, "cy": 577}])
check("R6 主城锚点首帧丢失 -> 重截后找到并走完导航",
      b._enter_pvp_hub("rank"), True)
check("R6 点了王者之巅 + 排位入口",
      [t[0] for t in b.taps if isinstance(t, tuple) and t and t[0] in ("rank_entry",)],
      ["rank_entry"])
check("R6 零返回键", len(b.backs), 0)

b = make_city_nav([None, None])
check("R7 主城锚点两帧均无 -> 失败收口", b._enter_pvp_hub("rank"), False)
check("R7 留 no_wangzhe_anchor 图", b.anomalies, ["no_wangzhe_anchor"])
check("R7 零返回键 (绝不点名主城按 back)", len(b.backs), 0)

# ============ Section S: 点击前按钮态复核 (2026-09-13 预研落地, arena 窗口首验) ============
print("\n--- Section S: 点击前按钮态复核 (_btn_still_challenge / stale_pre) ---")


def make_btnchk(ocr_texts, shot_ok=True):
    b = object.__new__(Battler)
    b._shot_img = lambda: IMG if shot_ok else None
    b._ocr_texts = lambda img, roi=None, scale=1.0: (
        [{"text": t, "box": [[0, 0]]} for t in ocr_texts])
    return b


# ROI 几何: 以按钮为中心 (±110, ±34), 钳制到画面内
captured = {}


def cap_ocr(img, roi=None, scale=1.0):
    captured["roi"] = roi
    captured["scale"] = scale
    return []


b = make_btnchk([])
b._ocr_texts = cap_ocr
b._btn_still_challenge((1500, 125))
r = captured["roi"]
check("S1 ROI 以按钮为中心且钳制画面内", (r[0] <= 1500 <= r[2] and r[1] <= 125 <= r[3]
                                          and r[2] <= 1600 and r[0] >= 0 and r[3] <= 900), True)
check("S1 y 半径收紧 (±34, 防串 62px 邻行)", (r[3] - r[1]) <= 68 + 1, True)

check("S2 见「修养中」-> False (正面证据)",
      make_btnchk(["修养中[12秒]"])._btn_still_challenge((1500, 125)), False)
check("S3 见「挑战」-> True",
      make_btnchk(["挑战"])._btn_still_challenge((1500, 125)), True)
check("S4 ROI 无文本块 -> None (不动作)",
      make_btnchk([])._btn_still_challenge((1500, 125)), None)
check("S5 仅乱码块 (无正面证据) -> None",
      make_btnchk(["修券中", "挑戓"])._btn_still_challenge((1500, 125)), None)
check("S6 截图失败 -> None",
      make_btnchk(["修养中"], shot_ok=False)._btn_still_challenge((1500, 125)), None)


def make_precheck(pre_val):
    b = object.__new__(Battler)
    b.fought = 0
    b.errors = 0
    b._cur_opp = None
    b.logs, b.taps = [], []
    b.stop_evt = threading.Event()
    b._db = type("DB", (), {"upsert_from_scan": staticmethod(lambda n: {"name": n})})()
    b._btn_still_challenge = lambda btn: pre_val
    b._tap = lambda *a: b.taps.append(a)
    b._progress = lambda *a, **k: None
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    return b


b = make_precheck(False)
check("S7 点击前复核=修养中 -> stale_pre (免点免等)",
      b._do_one_battle("1区 测试人", (1500, 125)), "stale_pre")
check("S7 零点击", len(b.taps), 0)

b = make_precheck(True)
b._wait_until = lambda *a, **k: None          # mock 无战斗页 -> 走 fatal 分支
b._shot_img = lambda: IMG
b._popup_text = lambda img: None
b._is_on_list = lambda img: False
b._save_anomaly = lambda tag: b.anomalies.append(tag)
b.anomalies = []
check("S8 复核=挑战 -> 照常点击 (fatal 仅因 mock 无战斗页)",
      b._do_one_battle("1区 测试人", (1500, 125)), "fatal")
check("S8 已点击 1 次", len(b.taps), 1)

check("S9 主循环 stale/stale_pre 同支短等重扫",
      'if st in ("stale", "stale_pre"):' in src, True)
check("S9 仅 stale (点击后) 拉黑, stale_pre 不拉黑",
      src.count("self._stale_names.add(name)") == 1
      and src.index('if st == "stale":\n                        self._stale_names.add(name)')
      > src.index('if st in ("stale", "stale_pre"):'), True)
check("S9 复核调用在点击之前",
      src.index("_btn_still_challenge(btn)") < src.index("self._tap(*btn)"), True)

# ------------- K. 窗口内自愈闸门 (2026-09-22 用户口径) -------------
# 用户原话: "只要游戏不在排位, 但是确实到我指定的排位时间了, 那就要进入到排位界面"。
# 旧实现五族自愈一律 `计数 > 3` 终止; 2026-09-22 18:42 因此被冤枉终止 (1 小时内游戏被
# 模拟器回收 4 次, 前 3 次都自愈成功), 窗口内空转 44 分钟。新契约: 窗口内不设次数上限,
# 改由"窗口是否还开着 + 窗口内是否长时间无进展"决定是否收尾。
print("\n=== K. 窗口内自愈闸门 (2026-09-22 口径) ===")
import sj_bot.state_machine as _SM

_saved_glory, _saved_arena = _SM.in_glory_window, _SM.in_window


def make_gate(mode, rescues=0, since=None):
    b = object.__new__(Battler)
    b.mode = mode
    b._selfheal_since = since
    b._last_stop_kind = ""
    b.nav_rescues = rescues
    b.logs = []
    b.log = lambda lv, msg: b.logs.append((lv, msg))
    b._sleep_stop = lambda s: None
    return b


try:
    # 1) 荣耀模式 + 窗口内 + 已自愈 9 次 -> 仍继续 (旧实现在这里早就 >3 终止了)
    _SM.in_glory_window = lambda now=None: True
    b = make_gate("rank_glory", rescues=9)
    check("K1 窗口内第 10 次自愈仍继续 (无硬上限)", b._selfheal_ok("nav_rescues", "x"), True)
    check("K1 计数已累到 10", b.nav_rescues, 10)

    # 2) 窗口已关 -> 立刻收尾, 不空转
    _SM.in_glory_window = lambda now=None: False
    b = make_gate("rank_glory")
    check("K2 窗口已关 -> 不再自愈", b._selfheal_ok("nav_rescues", "x"), False)
    check("K2 收尾原因 = window_closed", b._stop_reason("兜底"), "window_closed")

    # 3) 窗口内但无进展已超时 -> 判真故障收尾
    _SM.in_glory_window = lambda now=None: True
    b = make_gate("rank_glory", since=_B.time.time() - (_B.SELFHEAL_STALL_SEC + 60))
    check("K3 窗口内长时间无进展 -> 收尾", b._selfheal_ok("nav_rescues", "x"), False)
    check("K3 收尾原因 = selfheal_stall", b._stop_reason("兜底"), "selfheal_stall")

    # 4) 窗口内首次自愈 -> 记下无进展起点并继续
    b = make_gate("rank_glory")
    check("K4 窗口内首次自愈 -> 继续", b._selfheal_ok("nav_rescues", "x"), True)
    check("K4 已记下无进展起点", b._selfheal_since is not None, True)

    # 5) 真打成一局 = 有进展 -> 重置, 长时段挂机不会被误杀
    b._selfheal_progress()
    check("K5 打完一局重置无进展计时", b._selfheal_since, None)

    # 6) 争霸模式走同一个窗口判据 (in_window), 窗口外同样立刻收尾
    _SM.in_window = lambda now=None: False
    b = make_gate("arena")
    check("K6 争霸窗口外 -> 收尾", b._selfheal_ok("nav_rescues", "x"), False)
    check("K6 原因 = window_closed", b._stop_reason("兜底"), "window_closed")

    # 7) 日常排位没有窗口概念 -> 保持旧的次数上限语义 (不改变既有行为)
    b = make_gate("rank", rescues=3)
    check("K7 无窗口模式(rank) 仍按次数上限", b._selfheal_ok("nav_rescues", "x"), False)
    check("K7 原因 = selfheal_cap", b._stop_reason("兜底"), "selfheal_cap")
    b = make_gate("rank", rescues=1)
    check("K7b rank 第 2 次仍继续", b._selfheal_ok("nav_rescues", "x"), True)

    # 8) 放行时清掉残留收尾原因 (调用方不会读到上一条)
    _SM.in_glory_window = lambda now=None: True
    b = make_gate("rank_glory")
    b._last_stop_kind = "selfheal_cap"
    b._selfheal_ok("nav_rescues", "x")
    check("K8 放行时清空 _last_stop_kind", b._last_stop_kind, "")

    # 9) 退避有界 (无次数上限后靠它防热循环, 上限取末值)
    b = make_gate("rank_glory")
    seq = []
    b._sleep_stop = lambda s: seq.append(s)
    for n in (1, 2, 3, 4, 5, 99):
        b._selfheal_pause(n)
    check("K9 退避有界且取末值", seq, [2, 5, 10, 20, 30, 30])

    # 10) 兜底: 闸门没给原因时用调用方原语义码 (不能把原因吞掉)
    b = make_gate("rank_glory")
    check("K10 无闸门原因时回落到调用方码", b._stop_reason("no_battle_ui"), "no_battle_ui")
finally:
    _SM.in_glory_window, _SM.in_window = _saved_glory, _saved_arena

check("K11 _selfheal_ok 调用点 7 处 (五族)", src.count("_selfheal_ok("), 8)  # 7 调用 + 1 定义
check("K12 窗口内自愈策略常量齐备",
      all(k in src for k in ("SELFHEAL_STALL_SEC", "SELFHEAL_BACKOFF", "SELFHEAL_LEGACY_MAX")),
      True)

print(f"\n{sum(RESULTS)}/{len(RESULTS)} 通过")
sys.exit(0 if all(RESULTS) else 1)
