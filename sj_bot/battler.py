"""自动对局引擎 (adb 直驱, 无 uiautomator2 session 依赖, 长跑更稳)。

用途: Web 控制台的"任务"在后台线程里调本引擎跑一整段对局。
两种任务模式复用同一引擎:
  - mode='arena' 全民争霸赛: 受时段闸门限制(周六/日 16:00-17:00, 2026-09-11 修订),
                      周积分到 6000 自动收手, rounds=None 一直打,
                       直到手动停止/窗口结束/积分上限。
  - mode='rank'  排位赛: 用户给 rounds 场数(每场=一次完整匹配+比赛+领奖), 打满即停;
                       不受时段/周上限约束; 走固定序列 OCR 状态机, 不依赖对手名单/档案决策。

全民争霸(arena) 单场流程 (每处视觉量都已实测或标注 CALIB=首跑需标定):
  1) scan_opponent_list() 截列表 -> 每行玩家+挑战按钮坐标 (实测: 右侧竖列蓝青按钮);
     只有带「挑战」短块的行才成为候选 —— 行被打过就变「修养中」, 自然被排除;
  2) GameFlow.decide() 判定 battle/skip/cooldown/stop (规则已落定);
  3) battle: tap(挑战) -> 轮询等"跳过"出现(开局) [CALIB: 若存在确认弹窗]
     -> 等 ARENA_SKIP_AFTER_SEC(10s) -> tap(跳过, 结束战斗) [用户口径: 10s 后可跳];
     战斗页确认后才 mark_attack(对手进入 30s「修养中」) —— 被"行动力不足"挡住时不留残留状态;
  4) settle (2026-09-17 用户口径, 与排位结算链不同): 只有一个轻量弹窗 ——
     胜 = "加积分"弹窗 / 负 = "挑战失败"提示; 两者都点「返回」回到列表。
     判定顺序: 先认 ARENA_LOSS_HINTS(挑战失败 专有短语) -> 再认 ARENA_WIN_HINTS(胜利/积分)
     [CALIB: 首跑必须记录 OCR 实读文案, 见日志 "结算文案(arena)"]。
  5) 节奏: 无自身 CD —— 结算「返回」回到列表后**立刻**重扫并抢下一个可打的人
     (名单实时刷新, 谁被打进修养中、谁刚恢复都在变);
     行动力耗尽 -> 点挑战后不会进战斗页(仍停在列表 + 弹窗), 主循环等
     STAMINA_RECOVER_SEC(60s) 重试, 即"一分钟一把"; 连续多次仍无进展则存图终止待校准。

排位(rank) 单局流程 (固定序列, OCR 文字驱动; 假设任务启动时已在荣耀排位赛主页):
  1) 主页断言 (OCR 找"荣耀排位赛"标题);
  2) 点"单人匹配" (OCR 找文字, 回退固定槽位);
  3) 等比赛界面: BAND_ROI 出现"跳过" (倒计时结束自动进入);
  4) 立即点"跳过" (排位不等 10s);
  5) 等奖励页 (开启/领取/宝箱/+200 等) -> OCR 候选按钮按 y 聚类取第 3 簇最右;
  6) 等"比赛结束" -> 点击;
  7) 等"返回大厅" -> 点击 -> fought+=1, 进入下一局。

异常兜底: 任何阶段卡住 -> 存校准截图 captures/anomaly_*.png 并报错退出, 不瞎点。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from sj_bot.config import Config
from sj_bot.db import OpponentDB
from sj_bot import rank_layout as rk
from sj_bot.state_machine import (
    GameFlow,
    WEEKLY_POINTS_CAP,
    ARENA_INTERVAL_SEC,
    STAMINA_RECOVER_SEC,
    ATTACK_COOLDOWN_SEC,
    in_window,
)

# ---------------------------------------------------------------- OCR 引擎参数 (2026-09-13 根因修复)
# rapidocr_onnxruntime 1.2.3 的 Det 预处理默认 limit_type=min + limit_side_len=736:
# **短边 < 736 的输入会被放大到短边 736**, 而全图 1600x900 (短边 900) 反而不缩放 ——
# 于是小 ROI 比全图更贵。实测单次 ROI OCR 3~30s, 单局起步路径串行 8 次 = 46~60s,
# 点「单人匹配」时倒计时早已归零 -> 必被踢回主城 (用户 2026-09-13 反馈的竞态根因)。
#   max 只降不升: 小 ROI 保持原分辨率 (670x120 就是 670x120), 实测快 2~4.5x;
#   1632 > 屏宽 1600 => 全图 OCR 不缩放, 与旧配置行为**完全一致** (不丢小字判据)。
# 实测对照见 scripts/ocr_config_bench.py (8/8 判据命中一致, ROI 提速 2~4.5x)。
OCR_DET_LIMIT_TYPE = "max"
OCR_DET_LIMIT_SIDE_LEN = 1632

# ---------------------------------------------------------------- 实测坐标 (1600x900)
SKIP_FALLBACK = (1468, 820)    # 战斗页"跳过"按钮固定坐标 (实测)
X2_BTN = (1351, 837)           # 加速按钮(不主动使用, 保留供标定)
SETTLE_ROI = (200, 250, 1400, 760)   # 结算结果文字区域 (CALIB)
BAND_ROI = (1200, 755, 1600, 900)    # 右下角按钮带 (找 跳过/X2)
LIST_BTN_ROI = (1350, 60, 1600, 700)  # 挑战按钮竖带 (2026-09-13 放宽: 真实列表按钮实测 y 248~630, 旧下沿 400 会漏; 与 arena_layout.BTN_COLUMN_ROI 保持一致)
POPUP_ROI = (250, 180, 1350, 700)    # 弹窗文字区(避开顶栏/列表/底部按钮), 见 2026-09-11

# 结算关键词 (OCR 逐字块匹配, 用两字词降低误报; CALIB: 首跑按实际文案补)
WIN_HINTS = ("胜利", "获胜", "成功", "恭喜")
LOSS_HINTS = ("失败", "战败", "惜败", "未战胜", "遗憾")
DISMISS_HINTS = ("确定", "确认", "继续", "返回列表", "返回")

# ---- 全民争霸(arena) 结算弹窗 (2026-09-17 用户口述口径) ----
# 用户原话: "假如输了会有挑战失败的提示, 然后点击返回继续去打下一个人;
#            打赢了, 就会有加积分的弹窗, 也是点击返回打下一个人"。
# 与排位赛的"结算链"完全不同: arena 只有**一个**轻量弹窗, 两种结局都以「返回」收口。
# 判定顺序很关键: **先认「挑战失败」这个专有短语**, 再退到通用胜利词 ——
# 失败弹窗里也可能出现"积分"字样(本期战绩/积分说明), 只看通用词会两边都命中 -> 判不出来。
ARENA_LOSS_HINTS = ("挑战失败",)                 # 失败弹窗专有短语 (最优先)
ARENA_WIN_HINTS = ("挑战胜利", "获胜", "胜利", "恭喜", "积分")   # 胜利=加积分弹窗
# arena 弹窗收口按钮: 「返回」优先, 其余为兜底(文案可能微调)
ARENA_DISMISS_HINTS = ("返回", "确定", "确认", "继续")
# arena 弹窗文字 ROI: 弹窗居中, 避开顶部积分栏(y<180)与底部"传送门/积分榜"(y>800)
ARENA_POPUP_ROI = (250, 180, 1350, 800)

# 弹窗兜底关闭词: 刻意不含"确定/购买/兑换" —— 行动力不足弹窗里的"购买"要花钻石,
# 只认明确的取消/关闭类按钮; 找不到就存图报错, 绝不瞎点。
POPUP_CLOSE_HINTS = ("取消", "关闭", "知道了", "再想想", "不了", "否")
# 弹窗文字里出现这些词 => 判定为"行动力不足"类软性阻断 (仅用于日志说明)
STAMINA_POPUP_HINTS = ("行动力", "体力", "不足", "恢复", "购买")

WAIT_BATTLE_START = 20     # 点挑战后等待进入战斗页(秒) — 真正的战斗开局
WAIT_SETTLE = 25           # 点跳过/结束后等待结算出现(秒)
WAIT_BACK_LIST = 30        # 结算后等待回到列表(秒)
ARENA_SKIP_AFTER_SEC = 10  # arena: 进战斗页后等 10s 再点「跳过」(用户口径; 排位是立即跳)
RANK_INTERVAL_SEC = 30     # 排位模式也保守用 30s 节奏(可在首跑标定后调小)
SCAN_EMPTY_RETRY = 10      # 列表无人时重扫间隔(秒)
# arena: 名单实时刷新 (谁被打进「修养中」、谁刚恢复可打, 都在变), 无人可打时缩短重扫间隔,
# 以便在对手修养结束的第一时间抢到出手权 —— 用户口径"需要快速点击能打的人"。
ARENA_EMPTY_RETRY = 5
MAX_CONSECUTIVE_FAIL = 6   # 连续截图失败/不在列表页超限 -> 终止(防无界循环刷屏)
# 2026-09-11 新规: 点挑战后若未进战斗页, 视为"行动力不足"等软性阻断 ->
# 等 STAMINA_RECOVER_SEC 秒(1 点行动力)重试; 连续这么多次仍无进展 => 界面异常终止.
MAX_BLOCK_STREAK = 5   # 连续截图失败/不在列表页/点挑战连续无进展 超限 -> 终止(防无界循环刷屏)
STALE_RETRY_SEC = 8    # 2026-09-13: 无弹窗阻断 (行翻回修养中, 秒级翻转) 的短等重扫间隔
# 2026-09-17: 点击**前**复核发现目标行已翻回「修养中」时不等 8s —— 名单是实时刷新的,
# 此刻应立刻重扫去抢**别的**可打的人 (该行要等 ~30s 才恢复, 等它没意义)。
STALE_PRE_RETRY_SEC = 1.0

# ==================== 窗口内自愈策略 (2026-09-22 用户口径) ====================
# 用户原话: "只要游戏不在排位, 但是确实到我指定的排位时间了, 那就要进入到排位界面"。
#
# 旧实现的问题 (2026-09-22 18:0x~18:42 真机实锤): nav/session/game/main_city/match 五族
# 自愈**一律 `计数 > 3` 就终止**。当夜 MuMu 因内存不足 (实测可用物理仅 0.93GB/负载 94%)
# 在 1 小时内回收游戏进程 4 次, 前 3 次都自愈成功, 第 4 次撞上 session_rescues>3
# 直接终止 -> 荣耀窗口内空转 44 分钟(18:42~19:26), 正是"窗口还在却放弃了"。
#
# 新口径: **把"游戏不在排位"当成要修的状态, 而不是要放弃的结论** ——
#   · 有窗口的模式(rank_glory / arena): 只要窗口还开着, 次数不设硬上限;
#     改用「窗口内连续无进展时长」熔断 (SELFHEAL_STALL_SEC) —— 真故障(如需要人工登录)
#     仍会收尾, 但不会被几次内存抖动提前打死;
#   · 每次自愈之间按 SELFHEAL_BACKOFF 退避, 避免无上限后变成热循环刷屏;
#   · 窗口一关立刻以 window_closed 收尾, 不空转消耗;
#   · 无窗口概念的模式(日常排位 rank) 语义**不变**, 仍是次数上限 (SELFHEAL_LEGACY_MAX)。
SELFHEAL_STALL_SEC = 900                     # 窗口内连续 15 分钟没能打成一局 -> 判真故障
SELFHEAL_BACKOFF = (2, 5, 10, 20, 30)        # 连续自愈第 1..n 次的退避秒数(超出取末值)
SELFHEAL_LEGACY_MAX = 3                      # 无窗口模式(rank)的次数上限, 保持旧语义


def make_ocr():
    """按项目统一参数创建 RapidOCR. **自测/诊断脚本请一律用它, 不要直接 `RapidOCR()`**.

    原因 (2026-09-13 根因): rapidocr_onnxruntime 1.2.3 的默认 Det 预处理是
    `limit_type=min` + `limit_side_len=736`, 会把短边不足 736 的输入**放大**到 736,
    导致小 ROI 单次 OCR 要 3~30s (详见 OCR_DET_LIMIT_TYPE 注释).
    各处自建默认 OCR 会把同一个坑重新引回来 —— 所以统一到这里.
    """
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(det_limit_type=OCR_DET_LIMIT_TYPE,
                    det_limit_side_len=OCR_DET_LIMIT_SIDE_LEN,
                    det_model_path="")


class Battler:
    """单线程对局任务执行器。外部通过 stop_evt / log / on_progress 交互。"""

    def __init__(
        self,
        mode: str = "arena",
        rounds: Optional[int] = None,
        db: Optional[OpponentDB] = None,
        log: Optional[Callable] = None,
        stop_evt: Optional[threading.Event] = None,
        on_progress: Optional[Callable] = None,
    ) -> None:
        assert mode in ("arena", "rank", "rank_glory")
        self.mode = mode
        # rounds: rank=打满场数停; rank_glory=0 表示一直打到 20:00 或手动停止; arena=None 打到收手
        self.rounds = rounds
        self.cfg = Config()
        self.serial = self.cfg.serial or "127.0.0.1:16384"
        self.adb = self.cfg.adb_exe
        self._db = db or OpponentDB(self.cfg.db_path)
        self.flow = GameFlow(device=None, db=self._db)  # type: ignore[arg-type]
        # 本任务内「反复 stale」对手名单 (2026-09-13): OCR 显示挑战但点击始终进不了
        # 战斗的行 (首跑: 雪人夜樱花 同一按钮 5 连阻断), 不排除会无限撞同一行。
        self._stale_names: set[str] = set()
        self.log = log or (lambda _lvl, _msg: None)
        self.stop_evt = stop_evt or threading.Event()
        self.on_progress = on_progress or (lambda _p: None)
        self._ocr = None
        # 最近一帧截图缓存 (速度优化: _wait_hint 等轮询命中后直接复用最后一帧判胜负,
        # 避免每局多截 1-2 次图, 每次截图 ~1s)
        self.last_img: Optional[np.ndarray] = None
        # 2026-09-13 提速: 同一帧的 OCR 结果缓存 (详见 _ocr_texts 注释)。帧一换即整体失效,
        # 故不会与历史帧串味; 只缓存"当前帧", 额外内存占用可忽略。
        self._ocr_cache: dict = {}
        self._ocr_cache_frame: Optional[np.ndarray] = None

        # 统计
        self.fought = 0
        self.wins = 0
        self.losses = 0
        self.skips = 0
        self.errors = 0
        self.stamina_waits = 0    # 2026-09-11: 因"行动力不足"未进战斗而等待 60s 的次数
        self.cooldown_waits = 0   # 每日 10 把冷却已等待次数 (等满 15 分钟继续打)
        self._nav_page = None     # P0(09-11): 最近一次导航落点 (hub/battle/reward/...), 供主循环接管
        # P0(09-12): 本局起点是否已在结算链 (reward/end/back). None=正常从主页/战斗页开始.
        self._pre_settle_stage: Optional[str] = None
        self.nav_rescues = 0      # P0(09-12): "既不在主页也不在战斗页" 的自愈重导航计数
        # P0(2026-09-13): 游戏失联 (进程被回收/被切桌面) 的自愈拉起计数
        self.game_rescues = 0
        # P0(2026-09-13): 游戏会话失效 (token失效弹窗) / 停在登录页 的自愈计数
        self.session_rescues = 0
        self._nav_back_tried = False   # P0(09-12): 未知页面已按过一次系统返回键 (防死循环)
        # P0(2026-09-13 提速): 上一局刚走完"结算链 -> 返回大厅", 下一轮起点必然是排位主页.
        # 用它开"稳态快车道", 省掉 _detect_settle_stage + _is_in_battle + _check_daily_limit
        # (只对"来源不确定"的入口才有必要, 实测合计 ~15s OCR, 正是点击迟到的余量).
        self._at_hub_confident = False
        # 2026-09-11 新规: 全民争霸改为"行动力"机制 —— 有行动力连打(间隔 0),
        # 行动力耗尽后每分钟恢复 1 点 => 1 场/分钟。排位/荣耀仍用固定 30s 节奏。
        self.interval = ARENA_INTERVAL_SEC if mode == "arena" else RANK_INTERVAL_SEC
        self._block_shot_saved = False  # 软性阻断现场只存第一张图, 防 60s 一次刷屏
        # 阶段截图开关: 平时 False (每张 ~1.5s, 校准锚点时临时置 True)
        self.stage_shots = False
        # 2026-09-17: 最近一次 arena 结算弹窗的 OCR 实读文案 (首跑标定胜负关键词用)
        self._settle_text = ""
        # 2026-09-22 (窗口内自愈策略): 当前这轮"窗口内无进展"的起点时刻. None = 尚未进入
        # 自愈 / 刚有进展; 由 _selfheal_progress() 在真正打成一局时重置。
        self._selfheal_since: Optional[float] = None
        # 最近一次终止归类的**精确原因** (window_closed / selfheal_stall / selfheal_cap),
        # 供调用方写入 exit_status -> 页面"上次结果"一眼可见。
        self._last_stop_kind = ""

    # ================================================================ 设备原语
    @property
    def ocr(self):
        """RapidOCR 单例. **必须显式覆盖检测预处理参数** (2026-09-13 根因修复).

        默认配置 `Det.limit_type=min` + `Det.limit_side_len=736` 会把短边不足 736 的
        输入**放大**到短边 736:
            160x60   -> 1963x736   (面积 x12)
            670x120  -> 4108x736   (面积 x38)
            1000x220 -> 3345x736
        而全图 1600x900 (短边 900) 不缩放 -> 小 ROI 反而比全图慢, 实测单次 3~30s.
        改成 `max` (只降不升) + 1632 (> 屏宽 1600, 全图行为不变) 后 ROI 提速 2~4.5x.

        另: `det_model_path=''` 必须显式给 —— rapidocr 的 UpdateParameters 只要收到
        任何 det_* 参数就会读 `det_dict['model_path']`, 缺 key 直接 KeyError.
        """
        if self._ocr is None:
            self._ocr = make_ocr()
        return self._ocr

    def _progress(self, phase: str, detail: str = "") -> None:
        self.on_progress({
            "phase": phase, "opp": getattr(self, "_cur_opp", ""),
            "fought": self.fought, "wins": self.wins, "losses": self.losses,
            "skips": self.skips, "errors": self.errors, "detail": detail,
            "stamina_waits": self.stamina_waits,   # 行动力不足等待次数(2026-09-11 新规)
        })

    def _shot_img(self) -> Optional[np.ndarray]:
        """截图 -> BGR ndarray。transport 掉线时先 connect 再重试一次。"""
        for attempt in (0, 1):
            try:
                r = subprocess.run(
                    [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=20,
                )
                if r.returncode == 0 and r.stdout:
                    img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        self.last_img = img
                        self._ocr_cache_frame = img   # 新帧 -> OCR 缓存整体失效
                        self._ocr_cache = {}
                        return img
            except Exception:
                pass
            if attempt == 0:
                try:
                    subprocess.run([self.adb, "connect", self.serial],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass
        return None

    def _tap(self, x: int, y: int) -> None:
        """tap, transport 掉线时自动 connect 重试 (与 _shot_img 一致).
        之前没有重试, 导致 MuMu 空闲掉线后所有点击被静默吞掉——根因修复."""
        for attempt in (0, 1):
            try:
                r = subprocess.run(
                    [self.adb, "-s", self.serial, "shell", "input", "tap",
                     str(int(x)), str(int(y))],
                    capture_output=True, timeout=10,
                )
                if r.returncode == 0:
                    return
            except Exception:
                pass
            if attempt == 0:
                try:
                    subprocess.run([self.adb, "connect", self.serial],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass

    # ================================================== 游戏存活探测 (2026-09-13 新增)
    # 背景 (用户反馈 "程序这么脆弱…老是异常结束不行, 有没有方法加强一下"):
    # 引擎此前**完全没有"游戏还在不在前台"的概念** —— 全部判据都只认游戏内页面,
    # 于是模拟器桌面/黑屏被当成 `unknown`, 只能靠"等 200s 匹配超时 + 按返回键逃生"
    # 收场, 而这两个手段对桌面都无效。实测 09-13 11:38~11:43 一次: 游戏进程被 MuMu
    # 回收, 引擎毫无察觉, 白烧 4 分钟且 0 场, 最后报 no_battle_ui 终止。
    # 下面四个方法给它补上"存活感知 + 自愈拉起": 便宜的 pidof 用于热路径,
    # 完整的前台校验只在异常路径上跑 (dumpsys ~1s)。

    def _game_pid(self) -> Optional[str]:
        """游戏进程 pid。'' = 进程不存在; None = 查询失败 (不确定, 不可据此下结论)。

        说明: adb 本身故障时也会返回空 stdout, 因此**不能单独用本方法判死** ——
        调用方必须先用 `_foreground_pkg()` 确认 adb 通, 再采信本结果 (见 _check_game_health)。
        """
        for attempt in (0, 1):
            try:
                r = subprocess.run(
                    [self.adb, "-s", self.serial, "shell", "pidof", self.cfg.game_pkg],
                    capture_output=True, timeout=10,
                )
                out = (r.stdout or b"").decode("utf-8", "ignore").strip()
                return out            # 有则 pid, 无则 ''
            except Exception:
                pass
            if attempt == 0:
                try:
                    subprocess.run([self.adb, "connect", self.serial],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass
        return None

    def _foreground_pkg(self) -> Optional[str]:
        """当前前台应用包名。None = 查询失败 (不确定)。

        用 `dumpsys window | grep mCurrentFocus` (~0.8-1.5s), 输出形如
        `mCurrentFocus=Window{d2eebdd u0 app.lawnchair/app.lawnchair.LawnchairLauncher}`。
        优先于 pidof 作为"是否在游戏里"的判据 —— 进程活着但被切到桌面同样会让引擎抓瞎。
        """
        for attempt in (0, 1):
            try:
                r = subprocess.run(
                    [self.adb, "-s", self.serial, "shell",
                     "dumpsys window | grep mCurrentFocus"],
                    capture_output=True, timeout=15,
                )
                out = (r.stdout or b"").decode("utf-8", "ignore")
                m = re.search(r"mCurrentFocus=Window\{.*?\s([A-Za-z][\w.]*)/", out)
                if m:
                    return m.group(1)
                return None
            except Exception:
                pass
            if attempt == 0:
                try:
                    subprocess.run([self.adb, "connect", self.serial],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass
        return None

    def _check_game_health(self) -> str:
        """游戏存活状态: 'ok' / 'background' (进程在但不在前台) / 'gone' (进程没了) / 'unknown'。

        判死口径很保守: 必须 `_foreground_pkg()` **成功返回** (说明 adb 通、能读到窗口栈)
        且前台不是游戏包, 此时才采信 `_game_pid()` 的空结果。这样能避免"adb 掉线"
        被误判成"游戏死了" -> 对一场正在进行的战斗乱拉起、乱点。
        """
        pkg = self._foreground_pkg()
        if pkg is None:
            return "unknown"
        if pkg == self.cfg.game_pkg:
            return "ok"
        pid = self._game_pid()
        if pid is None:
            return "unknown"
        return "background" if pid else "gone"

    def _launch_game(self) -> bool:
        """把游戏拉回前台 (冷启动亦可)。用 monkey LAUNCHER 方式, 不依赖 uiautomator2 session。"""
        for attempt in (0, 1):
            try:
                r = subprocess.run(
                    [self.adb, "-s", self.serial, "shell", "monkey",
                     "-p", self.cfg.game_pkg,
                     "-c", "android.intent.category.LAUNCHER", "1"],
                    capture_output=True, timeout=25,
                )
                if r.returncode == 0:
                    return True
            except Exception:
                pass
            if attempt == 0:
                try:
                    subprocess.run([self.adb, "connect", self.serial],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass
        return False

    def _ensure_game_foreground(self, tag: str = "") -> bool:
        """确保游戏在前台; 进程被回收则重新拉起。返回 True = 已就绪可继续导航。

        'unknown' 视为就绪 —— 探测失败时不能凭猜测去拉起游戏 (可能打断正在进行的战斗),
        此时交给原有页面判据兜底, 保持旧行为不变。
        """
        state = self._check_game_health()
        if state == "ok":
            return True
        if state == "unknown":
            self.log("warn", f"游戏前台状态无法判定 (adb 查询失败), 按就绪继续 (tag={tag})")
            return True
        if state == "background":
            self.log("warn", f"游戏进程仍在但已不在前台 (被切到桌面/其它应用), 拉回前台 (tag={tag})")
        else:
            self.log("warn", f"游戏进程已不存在 (被模拟器回收?), 重新拉起游戏 (tag={tag})")
        if not self._launch_game():
            self.log("error", "拉起游戏失败 (adb start 未成功)")
            return False
        deadline = time.time() + 60
        while not self.stop_evt.is_set() and time.time() < deadline:
            if self._foreground_pkg() == self.cfg.game_pkg:
                self.log("ok", "✓ 游戏已回到前台, 等其加载 (冷启动首屏/登录约 6s)")
                self._sleep_stop(6)
                return True
            self._sleep_stop(3)
        self.log("error", "拉起游戏后 60s 内未回到前台")
        return False

    def _recover_from_game_loss(self) -> str:
        """游戏失联自愈: 拉起游戏 + 重导航回排位主页。

        返回 'recovered' (调用方可 continue 重试本局) / 'stop' (该收尾, 精确原因见
        `self._last_stop_kind`; 若为空表示拉起游戏本身失败, 调用方按原语义归类) / 'nav_fail'。
        2026-09-22 变更: "游戏反复被回收"不再用固定 3 次就放弃 —— 窗口内持续拉起+重导航
        (内存压力下被回收是**环境**问题, 不该判成任务的错), 见 `_selfheal_gate`。
        """
        if not self._selfheal_ok("game_rescues", "游戏失联"):
            return "stop"
        self._selfheal_pause(self.game_rescues)
        self.log("warn", f"游戏失联, 尝试拉起并重导航 (第 {self.game_rescues} 次)")
        self._save_anomaly("game_gone")
        if not self._ensure_game_foreground("recover"):
            return "stop"
        self._sleep_stop(3)
        if not self._enter_pvp_hub_retry("rank"):
            return "nav_fail"
        return "recovered"

    def _press_back(self) -> bool:
        """发送系统返回键 (keyevent 4), 用于退出误入的页面.

        场景 (2026-09-12): 引擎可能因误点/游戏自身跳转落到"荣耀排行""活动页"等
        未登记页面; 这类页面所有判据都不认 -> 导航判未知 -> 任务终止.
        系统返回键是这类页面的通用逃生手段: 退回上一层后通常就回到排位主页/主城,
        再走正常导航即可. 与 _tap 一样带 transport 掉线重连.
        注意: 只在"已确认不在战斗页"的路径上调用 —— 战斗页按返回会弹退出确认框.
        """
        for attempt in (0, 1):
            try:
                r = subprocess.run(
                    [self.adb, "-s", self.serial, "shell", "input", "keyevent", "4"],
                    capture_output=True, timeout=10,
                )
                if r.returncode == 0:
                    return True
            except Exception:
                pass
            if attempt == 0:
                try:
                    subprocess.run([self.adb, "connect", self.serial],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass
        return False

    # 逃生后可安全续跑的页面 (回到这些页 -> 主循环/导航能重新接管)
    _ESCAPE_OK_PAGES = ("main_city", "hub", "battle", "reward", "end", "back")

    def _escape_relaunch_game(self, tag: str) -> Optional[str]:
        """逃生-拉起游戏: 确保游戏回前台后重新分类, 回到已知页则返回页名。

        2026-09-13 18:0x 实锤后从 _escape_unknown_page 抽出: "游戏不在前台"不只出现在
        逃生入口, 还会在**逃生中途**出现 (主城按返回键弹退出确认框, 连按把游戏按退出
        到模拟器桌面, 此后返回键全部空转) —— 循环内也要能调同一套"拉起+重分类"。
        """
        if not self._ensure_game_foreground(f"escape_{tag}"):
            return None
        page = self._classify_page()
        self.log("warn", f"逃生({tag}) 拉起游戏后页面识别: {page}")
        if page in self._ESCAPE_OK_PAGES:
            self.log("ok", f"✓ 逃生成功: 拉起游戏后回到 {page}")
            return page
        if page in ("login", "session"):
            # 冷启动后游戏必然停在登录页 -> 顺手登录一次, 否则"拉起了但进不去"
            self.log("warn", f"逃生({tag}) 拉起后停在{page}, 先重新登录")
            rr = self._recover_from_session(f"escape_{tag}")
            return "hub" if rr == "recovered" else None
        return None

    def _escape_unknown_page(self, tag: str, tries: int = 4) -> Optional[str]:
        """通用逃生 (2026-09-12 用户需求): 落到不认识的页面时, 连按系统返回键退回已知页.

        背景 (用户反馈): 引擎有时会进入「地下城 → 副本关卡」这类页面, 所有判据都不认,
        等比赛界面 200s 超时后报 no_battle_ui 终止, 而且**根本不知道怎么退出**.
        用户给出两个方案, 本实现选**方案一** (未知页直接 back 退回主城继续), 理由:
          · 副本页/排行榜/活动页等"引擎不该在"的页面无法穷举 (实测同一批日志里
            既出现过副本章节图, 也出现过"等待二级菜单时画面已是战斗页"的错位状态),
            枚举入口 (方案二) 是不可闭合的;
          · 返回键是游戏内**通用**的向上退栈, 一次实现覆盖所有未知页, 且成本极低 (~0.3s/次).

        语义: 反复「按返回键 -> 等 1.5s -> 重新分类」, 一旦回到 _ESCAPE_OK_PAGES 任一页
        即返回该页名 (交调用方继续); 用尽 tries 仍不认 -> 返回 None (调用方才终止).
        安全: ① 已确认在战斗页时**不按** (战斗页按返回会弹退出确认框);
              ② 本方法只在"已判定为未知/副本页"的路径上调用, 不会误伤正常流程.
        """
        if self._is_in_battle():
            return "battle"
        # 游戏失联优先 (2026-09-13): 若"页面无法识别"的真实原因是游戏压根不在前台
        # (模拟器桌面/黑屏), 按返回键永远不会生效 —— 桌面不吃 back。实测 09-13 11:43
        # 连按 2 次返回键全部返回 unknown, 白耗按键与超时。先拉起游戏; 拉起后游戏
        # 通常直接回到主城/战斗页, 那就直接返回, 省掉整轮逃生。
        if self._check_game_health() in ("gone", "background"):
            self.log("warn", f"逃生({tag}): 无法识别的真实原因是游戏不在前台, 先拉起游戏")
            return self._escape_relaunch_game(tag)
        # 会话失效 / 停在登录页 (2026-09-13): 按返回键对这两种页面都无效
        # (模态弹窗不吃 back, 登录页按 back 可能直接退出游戏), 先重新登录再说.
        if self._is_session_expired() or self._is_login_screen():
            self.log("warn", f"逃生({tag}): 无法识别的真实原因是游戏会话失效/停在登录页, 先重新登录")
            rr = self._recover_from_session(f"escape_{tag}")
            return "hub" if rr == "recovered" else None
        for i in range(1, tries + 1):
            if self.stop_evt.is_set():
                return None
            self._press_back()
            self._sleep_stop(1.5)
            page = self._classify_page()
            self.log("warn", f"逃生({tag}) 第 {i}/{tries} 次按返回键 -> 页面识别: {page}")
            if page in self._ESCAPE_OK_PAGES:
                self.log("ok", f"✓ 逃生成功: 已从无法识别的页面退回 {page}")
                return page
            # 2026-09-13 18:0x 实锤 (荣耀任务 nav_failed 三连): 入口处那一次健康检查
            # 覆盖不到**中途**变化 —— 主城按返回键弹退出确认框, 连按把游戏按退出到
            # 模拟器桌面, 此后返回键空转、分类恒 unknown, 4 次白按 + 3 轮盲跑导航。
            # 每次返回后复查游戏健康; gone/background 改走"拉起游戏"逃生。
            # (探测失败=unknown 时不轻举妄动, 保持按返回键的旧行为)
            if self._check_game_health() in ("gone", "background"):
                self.log("warn", f"逃生({tag}): 返回键后游戏已不在前台, 改为拉起游戏")
                rel = self._escape_relaunch_game(tag)
                if rel is not None:
                    return rel
                # 拉起后仍不认: 桌面/登录页上继续按返回键同样无意义, 直接收口
                self._save_anomaly(f"escape_failed_{tag}")
                self.log("error", f"逃生失败: 拉起游戏后页面仍无法识别 (tag={tag})")
                return None
        self._save_anomaly(f"escape_failed_{tag}")
        self.log("error", f"逃生失败: 连按 {tries} 次返回键后页面仍无法识别 (tag={tag})")
        return None

    def _save_anomaly(self, tag: str) -> None:
        """异常现场存截图供人工/首跑标定。"""
        img = self._shot_img()
        if img is None:
            return
        p = Path(self.cfg.capture_dir) / f"anomaly_{_dt.datetime.now():%H%M%S}_{tag}.png"
        try:
            cv2.imencode(".png", img)[1].tofile(str(p))
            self.log("warn", f"异常现场已存: {p.name}")
        except Exception:
            pass

    def _save_reward_frame(self, verdict: Optional[str]) -> Optional[str]:
        """把「开箱奖励」弹窗的那一帧留下来, 并记一条结构化奖励明细。

        时机 (关键, 别挪): 只有在 `end` 环节**已经等到「比赛结束」按钮**时, `last_img`
        才是含奖励清单的弹窗 —— 刚点完宝箱那一瞬间还在开箱动画里, 抓到的是空弹窗。
        这里直接复用那帧, **不额外截图** (本机单次截图 ~1~13s, 每局多截一次不划算)。

        产物:
          captures/rewards/<日期>/<HHMMSS>.png    人工可看的原图 (胜负判出后由
                                                  _finalize_reward 补成 <HHMMSS>_win.png)
          data/rewards.jsonl                      结构化明细, 供前端展示

        为什么落 jsonl 而不是只留图: 用户要的是"打了这么久每次获得了什么奖励"的
        **聚合视图**, 图只能一张张翻; jsonl 才能按天汇总、在页面上列表展示。
        """
        img = getattr(self, "last_img", None)
        if img is None:
            return None
        ts = _dt.datetime.now()
        day_dir = Path(self.cfg.capture_dir) / "rewards" / f"{ts:%Y%m%d}"
        stamp = f"{ts:%H%M%S}"
        # 文件名**不带胜负**: 留图这一刻 verdict 还没判出来 (见 _finalize_reward), 带上
        # 只会得到 "_unknown", 补判后再改名又会变成 "_unknown_win" 这种丑东西。
        name = f"{stamp}.png"
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            cv2.imencode(".png", img)[1].tofile(str(day_dir / name))
        except Exception as e:                      # 落图失败不能打断结算链
            self.log("warn", f"奖励截图保存失败: {e!r}")
            return None

        rec = {
            "ts": f"{ts:%Y-%m-%d %H:%M:%S}",
            "mode": self.mode,
            "round": self.fought + 1,
            "verdict": verdict or "unknown",
            "file": str((day_dir / name).relative_to(self.cfg.capture_dir)),
        }
        try:
            rp = Path(self.cfg.data_dir) / "rewards.jsonl"
            with rp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        # 供结算链走完后 _finalize_reward() 补判胜负 (留图此刻 verdict 多半还是 unknown)
        self._last_reward_file = rec["file"]
        self.log("ok", f"开箱奖励已留图: {rec['file']}  (第 {rec['round']} 局)")
        return rec["file"]

    def _finalize_reward(self, verdict: Optional[str]) -> None:
        """结算链走完后**补判**本局胜负到刚记的那条开箱奖励上。

        为什么需要: 「开箱奖励」弹窗出现在 `end` 环节, 而本局胜负要么在该环节之前的
        结算页标题判出, 要么一直晚到 `back` 环节的战绩页大字才判出 ⇒ 留图那一刻
        `verdict` 常是 None (实测 2026-09-29 两局都是 unknown)。补判后:
          · `data/rewards.jsonl` 的 verdict 就地改对 (前端"胜/负"标才准);
          · 文件名补上 `_win`/`_loss` 后缀, 让人直接翻目录也能看出胜负。
        按 file 字段定位记录 —— 前端只认这一条, 改名后同步回写, 不会指丢。
        """
        rec_file = getattr(self, "_last_reward_file", None)
        if not rec_file or verdict not in ("win", "loss"):
            return
        self._last_reward_file = None                  # 一局只补一次
        try:
            rp = Path(self.cfg.data_dir) / "rewards.jsonl"
            if not rp.exists():
                return
            out, new_rel = [], None
            for ln in rp.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    out.append(ln)                     # 脏行原样保留, 不吞数据
                    continue
                if rec.get("file") == rec_file and rec.get("verdict") in (None, "", "unknown"):
                    old = Path(self.cfg.capture_dir) / rec_file
                    if old.exists():
                        newp = old.with_name(f"{old.stem}_{verdict}{old.suffix}")
                        try:
                            old.rename(newp)
                            new_rel = str(newp.relative_to(self.cfg.capture_dir))
                            rec["file"] = new_rel
                        except OSError:
                            pass                       # 改名失败不算错, verdict 照样改对
                    rec["verdict"] = verdict
                out.append(json.dumps(rec, ensure_ascii=False))
            tmp = rp.with_name(rp.name + ".tmp")
            tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
            tmp.replace(rp)                            # 原子替换, 断电不会留半个文件
            if new_rel:
                self.log("info", f"开箱奖励补判胜负: 本局{'胜' if verdict == 'win' else '负'} "
                                 f"-> {new_rel}")
        except Exception as e:                          # 补判失败绝不能影响收尾
            self.log("warn", f"开箱奖励补判失败: {e!r}")

    # ================================================================ OCR 工具
    def _ocr_texts(self, img: np.ndarray, roi: tuple | None = None,
                   scale: float = 1.0) -> list[dict]:
        """OCR -> [{text, cx, cy, score, area}] (坐标已还原到原图)。

        2026-09-13 提速: 同一帧上同一 (roi, scale) 只真正 OCR 一次, 结果缓存复用。
        单局起步路径会对**同一个主页按钮 ROI** 反复 OCR (主页断言 / 稳态快车道 /
        点单人匹配 各一次, 关键词集还各不相同) —— 实测单次 ROI OCR 3~8s, 重复调用
        正是"点单人匹配迟到 46~60s、倒计时归零被踢回主城"的直接来源。
        缓存以**同一帧对象**为界 (身份比较 `is`, 而非 id), 帧一换 (_shot_img) 即整体失效,
        因此不会把旧帧结果串到新帧上; 传入历史帧时 `img is not self._ocr_cache_frame`,
        自动绕过缓存, 行为与旧版一致。"""
        cached = getattr(self, "_ocr_cache_frame", None) is img
        key = (roi, scale)
        if cached:
            hit = self._ocr_cache.get(key)
            if hit is not None:
                return hit
        if roi is not None:
            x0, y0, x1, y1 = roi
            work = img[y0:y1, x0:x1]
        else:
            x0 = y0 = 0
            work = img
        if scale != 1.0:
            work = cv2.resize(work, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_CUBIC)
        try:
            result, _ = self.ocr(work)
        except Exception:
            return []
        out = []
        if not result:
            if cached:
                self._ocr_cache[key] = out
            return out
        for box, text, score in result:
            if not (isinstance(text, str) and text.strip()):
                continue
            pts = []
            for pt in box:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2 \
                        and isinstance(pt[0], (int, float)) and isinstance(pt[1], (int, float)):
                    pts.append((pt[0] / scale + x0, pt[1] / scale + y0))
            if len(pts) < 4:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append({
                "text": text.strip(),
                "cx": (min(xs) + max(xs)) / 2,
                "cy": (min(ys) + max(ys)) / 2,
                "x0": min(xs), "y0": min(ys),
                "area": (max(xs) - min(xs)) * (max(ys) - min(ys)),
                "score": float(score) if isinstance(score, (int, float)) else 0.0,
            })
        if cached:
            self._ocr_cache[key] = out
        return out

    def _find_hint(self, img: np.ndarray, hints: tuple[str, ...],
                   roi: tuple | None = None, scale: float = 1.0,
                   min_score: float = 0.0) -> Optional[dict]:
        """在 OCR 结果里找命中最优(score 最高, 面积最大的)关键词块。"""
        best: Optional[dict] = None
        for it in self._ocr_texts(img, roi=roi, scale=scale):
            if it["score"] < min_score:
                continue
            t = it["text"]
            for h in hints:
                if h in t:
                    if best is None or (it["score"], it["area"]) > (best["score"], best["area"]):
                        best = dict(it)
                        best["match"] = h
                    break
        return best

    def _wait_until(self, fn: Callable, timeout: float, interval: float,
                    desc: str, tick: Optional[Callable] = None) -> Optional:
        """轮询 fn() 直到返回真值/超时。可被 stop 打断; tick 在每次空转前回调(用于上报等待心跳)。"""
        deadline = time.time() + timeout
        while not self.stop_evt.is_set():
            val = fn()
            if val:
                return val
            if time.time() >= deadline:
                return None
            if tick is not None:
                try:
                    tick()
                except Exception:
                    pass
            time.sleep(interval)
        return None

    def _sleep_stop(self, sec: float) -> None:
        """可被停止信号打断的分片 sleep(空闲等待用, 停止响应更及时)."""
        while sec > 0 and not self.stop_evt.is_set():
            time.sleep(min(1.0, sec))
            sec -= 1.0

    def _save_stage(self, tag: str) -> None:
        """覆盖式保存当前截图到 captures/rank_<tag>.png (排位赛阶段截图).
        仅供窗口期首跑校准关键词/坐标用; 平时默认关闭 (每次截图 ~1.5s, 单局 6 张
        = 每局 ~9s 纯开销). 需要校准时把 self.stage_shots 置 True 再跑."""
        if not getattr(self, "stage_shots", False):
            return
        img = self._shot_img()
        if img is None:
            return
        p = Path(self.cfg.capture_dir) / f"rank_{tag}.png"
        try:
            cv2.imencode(".png", img)[1].tofile(str(p))
        except Exception:
            pass

    # ================================================================ 单场对局
    def _is_on_list(self, img: np.ndarray) -> bool:
        """是否回到对战列表: 挑战按钮竖带内出现 **>=2 个独立短「挑战」文本块**。

        2026-09-13 arena 首跑双翻车复盘 (证据见 reports/nav_perf_20260913.md §12):
        - 旧判据 `蓝青矩形 and 子串'挑战'` **两个方向都会翻车**:
          ① 主城右下活动面板「每日挑战1次竞技场」长句含'挑战'子串, 蓝色「详情」
            按钮也落在旧 ROI 内 ⇒ **主城被误判成列表**, 引擎空转 8 分钟不导航;
          ② 真实列表按钮实测在 y 248~630, 低于旧 ROI 下沿 400 ⇒ **真列表被判否**
            (anomaly_160810_not_list.png 拍的就是列表页);
          ③ 色块 mask 同样不可靠: 半透明「疯狂攻击已结束」横幅叠加下, 真实列表
            0 个矩形 (旧列表帧 9 个) ⇒ mask 不能再当必要条件。
        - 新判据: OCR 短块 (text=='挑战', 或前缀'挑战'且 ≤4 字) 计数 >= 2 ——
          列表每行一个独立按钮文字; 主城长句/二级菜单/排位主页实测均为 0。
          实测分布: 真列表 4 与 9 / 主城 0 / 菜单 0 / 主页 0。
        """
        n = 0
        for it in self._ocr_texts(img, roi=LIST_BTN_ROI, scale=1.0):
            t = it["text"].strip()
            if t == "挑战" or (t.startswith("挑战") and len(t) <= 4):
                n += 1
                if n >= 2:
                    return True
        return False

    # ---- 2026-09-11: "行动力不足"等软性阻断识别 (新规则不再有 30s 单场 CD) ----
    def _popup_text(self, img: np.ndarray) -> str:
        """弹窗文字区 OCR 摘要。仅用于日志: 说明被什么提示挡住了。"""
        try:
            parts = [
                it["text"] for it in self._ocr_texts(img, roi=POPUP_ROI, scale=1.0)
                if it["score"] >= 0.5
            ]
        except Exception:
            return ""
        return " / ".join(parts[:6])

    def _looks_like_stamina(self, txt: str) -> bool:
        """弹窗文字是否像"行动力不足"类提示 (行动力/体力 + 不足/恢复/购买/上限)。"""
        if not txt:
            return False
        return (any(k in txt for k in ("行动力", "体力"))
                and any(k in txt for k in ("不足", "恢复", "购买", "上限")))

    def _close_popup(self, img: np.ndarray) -> bool:
        """关闭弹窗: 只点明确的取消/关闭类按钮。

        刻意不含"确定/购买": 行动力不足弹窗的主按钮多是"购买行动力"(花钻石),
        误点会扣钻石 —— 宁可不关, 交给主循环等 60s 重试并留图人工校准。
        """
        hit = self._find_hint(img, POPUP_CLOSE_HINTS, roi=POPUP_ROI, scale=1.0)
        if hit is None:
            return False
        self.log("info", f"关闭弹窗({hit.get('match')}) @({hit['cx']:.0f},{hit['cy']:.0f})")
        self._tap(hit["cx"], hit["cy"])
        time.sleep(0.8)
        return True

    def _btn_still_challenge(self, btn: tuple[int, int]) -> Optional[bool]:
        """点击前复核目标按钮是否仍是「挑战」(2026-09-13 预研落地, arena 窗口真机首验)。

        背景: 列表行在 修养中⇄挑战 间**秒级翻转**, 扫描→点击之间的延迟都会竞态。
        条带扫描 (SCAN_BAND) 已把「扫描→选人」窗口从 13~21s 压到 4~7s; 本复核在
        点击前再做最后一道确认 (~1s 小 ROI OCR), 把窗口压到 ~1s。

        返回: True = 仍是挑战 (放行点击) / False = 已翻回修养中 (免点免等 20s+) /
              None = 判定不了 (截图失败 / ROI 无文本块 / 其他)。
        铁律: **只凭正面证据动作** —— 复核自身故障绝不改变既有行为 (False 需要
        「修养中」这个词的正面证据, 它不可能出现在可点击按钮上)。
        判据: 以按钮为中心的小 ROI (±110, ±34 —— 行距 ~62px, y 必须收紧防串邻行),
              含「挑战」短块 => True; 含「修养中」=> False; 其余 => None。
        """
        img = self._shot_img()
        if img is None:
            return None
        x, y = btn
        roi = (max(0, x - 110), max(0, y - 34), min(1600, x + 110), min(900, y + 34))
        texts = [it["text"].strip() for it in self._ocr_texts(img, roi=roi, scale=2.0)]
        if not texts:
            return None
        if any("修养中" in t for t in texts):
            return False
        if any(t == "挑战" or (t.startswith("挑战") and len(t) <= 4) for t in texts):
            return True
        return None

    def _do_one_battle(self, name: str, btn: tuple[int, int]) -> str:
        """对一名对手打一场并回写。

        返回:
          "ok"      - 正常完成(含结算识别不出但已走完流程)
          "blocked" - 点挑战后未进入战斗页且**有弹窗**(行动力不足等), 主循环等 60s 重试
          "stale"   - 未进入战斗页且**无弹窗** => 目标行翻回「修养中」(秒级翻转竞态),
                      主循环短等 8s 重扫 + 本任务拉黑该对手 (2026-09-13 首跑标定)
          "stale_pre" - **点击前**复核已翻回修养中 (免点免等, 2026-09-13 预研落地):
                      2026-09-17 改为只等 STALE_PRE_RETRY_SEC(1s) 立刻重扫 —— 名单是
                      实时刷新的, 该行要修养 ~30s, 应该马上去抢**别的**可打的人;
                      **不拉黑**对手 (行秒级翻回, 是好目标; 竞态熔断 _stale_streak 照常计数)
          "fatal"   - 界面未知/卡死 => 终止任务, 不瞎点
        """
        # 先用档案库归一化(OCR 错字 -> 已有档案名), 避免错字变体重复建档
        # 注: 属性名是 self._db (2026-09-11 修复: 原写成 self.db -> AttributeError,
        #     会让全民争霸第一场就崩, 此前 arena 只跑过列表扫描故未暴露)
        row = self._db.upsert_from_scan(name)
        name = row["name"]
        self._cur_opp = name
        self.log("info", f"[{self.fought + 1}] 挑战 {name} @按钮({btn[0]},{btn[1]})")
        self._progress("battle", f"挑战 {name}")
        t_start = time.time()
        # 点击前按钮态复核 (2026-09-13 预研落地): 修养中⇄挑战秒级翻转竞态的最后一道闸。
        # 只凭正面证据动作 (见 _btn_still_challenge); 故障/不确定一律放行, 不改变原行为。
        if self._btn_still_challenge(btn) is False:
            self.log("info", f"{name} 点击前复核: 按钮已翻回「修养中」, 免点免等, 短等重扫")
            return "stale_pre"
        self._tap(*btn)

        # ---- 等待进入战斗页: 右下角出现"跳过" ----
        def find_skip():
            img = self._shot_img()
            if img is None:
                return None
            return self._find_hint(img, ("跳过",), roi=BAND_ROI, scale=2.0)

        hit = self._wait_until(find_skip, WAIT_BATTLE_START, 1.5, "等待战斗开局")
        if hit is None:
            # 没等到战斗页。两种可能:
            #   a) 挑战根本没发出去(行动力耗尽/活动未开启/对手保护) -> 仍停在列表;
            #   b) 已离开列表但战斗 UI 认不出 -> 真异常, 必须终止。
            img = self._shot_img()
            if img is None:
                self.log("error", "点挑战后截图失败, 中止本场")
                self.errors += 1
                return "fatal"
            popup_txt = self._popup_text(img)
            still_list = self._is_on_list(img)
            if still_list or popup_txt:
                kind = "行动力不足" if self._looks_like_stamina(popup_txt) else "未进入战斗(软性阻断)"
                if not self._block_shot_saved:
                    self._save_anomaly("arena_blocked")
                    self._block_shot_saved = True
                self.log("warn", f"{kind}: 点挑战后仍停在列表页"
                                 f"(弹窗文字={popup_txt or '无'!r}); 不消耗对手冷却",
                                 )
                self._close_popup(img)          # 只喂"取消/关闭", 绝不点购买
                # 2026-09-13 首跑标定: 「修养中[N秒]」行在 修养中⇄挑战 间**秒级翻转**,
                # 扫描到点击之间隔一次全图 OCR (13~21s), 行可能已翻回修养中 ——
                # 这种阻断**没有弹窗** (anomaly_162644_arena_blocked.png: 目标行显示
                # 修养中[2秒], 弹窗文字='无'), 等行动力 60s 完全不对症。
                # 有弹窗才可能是行动力提示 (等 60s); 无弹窗 => 返回 stale 让主循环短等重扫。
                return "stale" if not popup_txt else "blocked"
            self.log("error", "点挑战后既未进入战斗页也未停在列表(界面未知), 中止本场")
            self._save_anomaly("no_battle")
            self.errors += 1
            return "fatal"

        # ---- 战斗页已确认: 此刻才登记 10 分钟冷却 ----
        # (2026-09-11: 移到"确认进战斗"之后再记, 避免被行动力挡住的空点占用
        #  对手冷却配额, 把那 1 次/分钟的出手机会浪费在冷却上)
        self.flow.on_battle_start(name)

        # ---- 进战斗页后等 10s 再点「跳过」(用户口径; arena 无自身 CD, 但需给自动战斗留时间) ----
        waited = time.time() - t_start
        if waited < ARENA_SKIP_AFTER_SEC:
            time.sleep(ARENA_SKIP_AFTER_SEC - waited)
        self.log("info", f"已到 {ARENA_SKIP_AFTER_SEC}s, 点击 跳过 结束战斗")
        self._tap(hit["cx"], hit["cy"])          # 用识别到的按钮中心, 回退固定点
        self._progress("settle", "战斗结束, 等待结算")

        # ---- 结算识别: arena 只有一个轻量弹窗 (胜=加积分 / 负=挑战失败), rank 走结算链 ----
        def find_verdict():
            img = self._shot_img()
            if img is None:
                return None
            if self.mode == "arena":
                # 先认「挑战失败」专有短语, 再退到通用胜利词:
                # 失败弹窗里也可能带"积分"字样, 只按通用词会两边都命中 -> 判不出胜负。
                texts = [it["text"] for it in self._ocr_texts(img, roi=ARENA_POPUP_ROI, scale=1.5)
                         if it["score"] >= 0.5]
                self._settle_text = " / ".join(texts[:8])   # 首跑标定: 记录 OCR 实读文案
                if any(any(h in t for h in ARENA_LOSS_HINTS) for t in texts):
                    return "loss"
                if any(any(h in t for h in ARENA_WIN_HINTS) for t in texts):
                    return "win"
                return None
            win = self._find_hint(img, WIN_HINTS, roi=SETTLE_ROI, scale=1.5)
            loss = self._find_hint(img, LOSS_HINTS, roi=SETTLE_ROI, scale=1.5)
            if win and not loss:
                return "win"
            if loss and not win:
                return "loss"
            return None

        result = self._wait_until(find_verdict, WAIT_SETTLE, 2.0, "结算判定")
        if self.mode == "arena":
            # 验收点④: 首跑必须能看到 OCR 实际读到的弹窗文案 (关键词标定依据)
            self.log("info", f"结算文案(arena): {self._settle_text or '(OCR 无文本块)'} "
                             f"-> 判定 {result or '未知'}")
        if result is None:
            self.log("warn", "结算弹窗未识别出明确胜负(关键词均未命中), 按无结果处理不写档案")
            self._save_anomaly("settle_unknown")
            self.errors += 1
        else:
            if result == "win":
                self.flow.on_settle(name, "win")
                self.wins += 1
                self.log("ok", f"✓ 战胜 {name} (积分弹窗 -> 返回), 周积分 +80")
            else:
                self.flow.on_settle(name, "loss")
                self.losses += 1
                self.log("warn", f"✗ 挑战失败 {name} (挑战失败提示 -> 返回) → 记一败 "
                                 f"(连败2次将入打不过名单)")
        self.fought += 1
        self._progress("settle", f"结果: {result or '未知'}")

        # ---- 关闭弹窗并回到列表: arena 两种结局都点「返回」 ----
        def find_dismiss():
            img = self._shot_img()
            if img is None:
                return None
            if self.mode == "arena":
                return self._find_hint(img, ARENA_DISMISS_HINTS,
                                       roi=ARENA_POPUP_ROI, scale=1.5)
            return self._find_hint(img, DISMISS_HINTS, roi=(300, 500, 1300, 860), scale=1.5)

        back = self._wait_until(find_dismiss, 8, 1.5, "结算关闭按钮")
        if back:
            self._tap(back["cx"], back["cy"])
            time.sleep(1.5)

        def on_list_now():
            img = self._shot_img()
            return self._is_on_list(img) if img is not None else None

        on_list = self._wait_until(on_list_now, WAIT_BACK_LIST, 2.0, "回到列表")
        if not on_list:
            self.log("error", "结算后未能回到对战列表(可能退回主菜单), 中止任务避免乱点")
            self._save_anomaly("lost_in_nav")
            self.errors += 1
            return "fatal"

        # ---- 节奏: arena 无自身 CD (2026-09-17 用户口述), interval=0 -> 立刻重扫抢下一人 ----
        # 行动力耗尽的情形在"未进入战斗页"分支处理(等 60s), 这里不做任何固定等待。
        remain = self.interval - (time.time() - t_start)
        if remain > 0:
            self.log("info", f"节奏等待 {remain:.0f}s 后打下一场")
            self._progress("cooldown", f"等待 {remain:.0f}s")
            while remain > 0 and not self.stop_evt.is_set():
                time.sleep(min(1.0, remain))
                remain -= 1.0
        return "ok"

    # ================================================================ 排位模式固定序列
    # (主页独有 OCR 锚点已迁移至 sj_bot.rank_layout.HUB_TITLE_ROI / HUB_UNIQUE_HINTS)

    def _is_rank_hub(self, img: np.ndarray, fast: bool = False) -> bool:
        """判定是否在荣耀排位赛主页:
        - fast=True (循环内每局用): 只在标题 ROI 找"荣耀排位赛" + 按钮 ROI 找匹配按钮
          (ROI 裁剪, 比全图快 ~3-5x; 返回大厅后主页必然就绪, 双 ROI 足够)
        - fast=False (导航终点断言用): 顶部标题 ROI + 主页独有元素**条带**任一
          防止把二级菜单里'荣耀排位赛'按钮误当成主页标题。

        2026-09-13 提速: fast=False 的"独有元素"由**全图**改为 `HUB_STRIP_ROI` 条带
        (y 270~670, 已实测覆盖全部主页独有元素)。全图 OCR 实测 15~53s, 是本条判据
        也是"被踢回主城后重导航 ~88s"的最大单项; 条带约 1/2.2 面积, 实测 8~25s。
        等价性由 `scripts/verify_hub_strip.py` 在 17 帧真实截图上验证: **全图与条带结论
        逐帧一致** (含"二级菜单/排行榜/reward/end/back/battle/arena列表/主城/登录页/桌面"负样本)。

        ⚠️ 不要把 fast=False 换成 fast=True 来提速 (2026-09-13 **已实测否决**):
        `scripts/verify_hub_fast.py` 在 22 帧上对比, 21 帧一致, 但 **`rank_1_matched.png`
        (匹配中状态) 条带=True 而 fast=False** —— 匹配中时三按钮行被"匹配中"提示替换,
        HUB_BTN_ROI 只剩 1 个文本块且不含任何按钮词 (加高到 h=300 也一样, +0 命中),
        真正命中条带的是 y≈580 的「更多排行」。而"匹配中"正是导航落点的高频状态,
        用 fast 会把它判成非主页 → 误入 unknown/重试。故 fast=True 只能用于
        "确认已在主页后的稳态复用" (主循环快车道), 不能用于 _classify_page 的落点断言。"""
        if self._find_hint(img, ("荣耀排位赛",), roi=rk.HUB_TITLE_ROI, scale=1.0) is None:
            return False
        if fast:
            return self._find_hint(img, rk.HUB_UNIQUE_HINTS, roi=rk.HUB_BTN_ROI, scale=1.0) is not None
        return self._find_hint(img, rk.HUB_UNIQUE_HINTS, roi=rk.HUB_STRIP_ROI, scale=1.0) is not None

    # 主城独有元素: 底部 4-tab 导航(英雄/装备/技能/背包) + 建筑/日常提示
    # 命中 ≥2 个视为在主城. 用于 antibot 答后区分"答错被踢回主城" vs
    # "答对进入匹配/战斗 (光球特效遮住主页标题)".
    _MAIN_CITY_HINTS = (
        # 底部 4-tab 导航 (最稳的常驻元素)
        "英雄", "装备", "技能", "背包",
        # 主城建筑
        "王者之巅", "矿洞", "英雄城堡", "铁匠铺", "地下城", "藏宝之地",
        "深渊迷宫", "魔龙", "荣誉殿堂",
        # 日常提示
        "每日任务", "每日挑战", "每日充值", "充值有礼",
    )
    _MAIN_CITY_MIN_HITS = 2

    def _is_menu_overlay(self, img: Optional[np.ndarray] = None) -> bool:
        """是否在主城的「设置浮层」(MOD包/账号/图鉴/音效/音乐/退出 六宫格模态菜单)。

        2026-09-23 用户提出 + 真机复现: 在主城**按一次系统返回键**就会弹出这个浮层,
        再按一次 back 退回主城。它不是"未知页", 但**必须先被认出来**, 否则:
        浮层是半透明的, 其下主城建筑文字(荣誉殿堂/铁匠铺/王者之巅)仍能被 OCR 读出,
        `_is_in_main_city` 的全图回退会命中 ≥2 个 `_MAIN_CITY_HINTS` ⇒ 误判 'main_city'
        ⇒ `_enter_pvp_hub` 去点被遮住的「王者之巅」⇒ 盲点无效 ⇒ nav_failed;
        而且因为分类不是 'unknown', 逃生分支不会触发 —— 一个 back 就能出的页面反而卡死。

        判定方式: ROI OCR + **关键词去重计数** ≥ `rk.MENU_MIN_HITS`。
        用去重计数而非块数, 与 `_is_login_screen` 同一理由 (det 可能把相邻标签并成一个乱码块)。
        成本: 1 次 ROI OCR (实测 1.8~3.3s)。调用点只在 tab 快路径**未命中**之后,
        故真主城不受影响; 浮层命中后反而省掉 10~15s 的全图 OCR。
        """
        if img is None:
            img = self._shot_img()
        if img is None:
            return False
        hits: set[str] = set()
        for it in self._ocr_texts(img, roi=rk.MENU_ROI, scale=1.0):
            for h in rk.MENU_HINTS:
                if h in it["text"]:
                    hits.add(h)
            if len(hits) >= rk.MENU_MIN_HITS:
                return True
        return False

    def _is_in_main_city(self, img: np.ndarray) -> bool:
        """判定是否在主城 (登录后默认世界地图页): 命中 ≥2 个主城独有元素.
        性能: 只做一次全图 OCR 再对全部提示词匹配 (旧实现每提示词各 OCR 一次,
        同一张图最多重复 13 次, 战斗场景下单次全图 OCR ~31s -> 卡等待循环数分钟).

        2026-09-13 提速: 先走**底部 tab 导航条**快路径 (`MAIN_CITY_TAB_ROI`, 200px 高,
        实测 0.3~0.9s vs 全图 10~15s)。主城底部常驻 tab, **去重后**命中
        ≥`MAIN_CITY_TAB_MIN_HITS` 个不同 tab 词即判主城; 不命中才退回全图判据 ——
        即"快路径只加速, 不改变召回口径"。
        (导航从主城出发时每次都要判一次, 这条快路径直接省下 ~10s/次。)
        ⚠️ 该 ROI 初版只有 90px 高, 在本模型上**恒返回 0 块**(静默失效), 详见
        rank_layout.MAIN_CITY_TAB_ROI 处的踩坑记录 —— 扁条 ROI 是不可靠的, 别照抄。"""
        tab_hits: set[str] = set()
        for it in self._ocr_texts(img, roi=rk.MAIN_CITY_TAB_ROI, scale=1.0):
            t = it["text"]
            for h in rk.MAIN_CITY_TAB_HINTS:
                if h in t:
                    tab_hits.add(h)
            if len(tab_hits) >= rk.MAIN_CITY_TAB_MIN_HITS:
                return True
        # 2026-09-23 新增: tab 未命中时先排除「设置浮层」模态, 再走全图回退。
        # 必须放在全图回退**之前** —— 浮层半透明, 其下主城建筑文字仍可被读出, 全图回退
        # 会稳定误判成主城(实测命中 荣誉殿堂/铁匠铺/王者之巅 3 个)。详见 _is_menu_overlay。
        # 顺带收益: 浮层帧在这里就 return False, 省掉后面 10~15s 的全图 OCR。
        if self._is_menu_overlay(img):
            self.log("warn", "检测到主城设置浮层 (MOD包/账号/…/退出) —— 不是主城, 需按返回键退出")
            return False
        hits = 0
        for it in self._ocr_texts(img, scale=1.0):
            t = it["text"]
            for h in self._MAIN_CITY_HINTS:
                if h in t:
                    hits += 1
                    break
            if hits >= self._MAIN_CITY_MIN_HITS:
                return True
        return False

    def _is_pvp_submenu(self, img: np.ndarray) -> bool:
        """是否在王者之争/勇者无惧/超级联赛二级菜单."""
        return self._find_hint(img, ("王者之争",), scale=1.0) is not None

    def _tap_rank_entry(self, img: np.ndarray) -> None:
        """二级菜单 -> 点「荣耀排位赛」卡片进入主页 (动态定位, 2026-09-13 P3).

        三级降级: 丝带模板 (~0.07s) -> OCR 全图 (与 _is_pvp_submenu 同参数, 同帧缓存
        命中则近乎免费) -> 固定热区 rk.RANK_ENTRY_HOTSPOT 兜底。可用性不降级:
        模板/OCR 任一失效都还有兜底, 不会比旧纯硬编码差。

        依据 (真机实验): 丝带标题本身**不可点** (点 (613,518) 无效), 可点区在
        标题中心 +87px; 模板正样本 0.869~1.000 / 负样本上界 0.481
        (见 rk.RANK_ENTRY_TPL_MIN 注释)。OCR/模板双失效会打 warn —— 模板漂移探针。
        """
        hit = self._find_btn_tpl(img, "rank_entry_ribbon.png", rk.RANK_ENTRY_ROI,
                                 min_score=rk.RANK_ENTRY_TPL_MIN)
        if hit:
            ex, ey, src = int(hit["cx"]), int(hit["cy"]) + rk.RANK_ENTRY_DY, "模板"
        else:
            h = self._find_hint(img, ("荣耀排位赛",), scale=1.0)
            if h is not None:
                ex, ey, src = int(h["cx"]), int(h["cy"]) + rk.RANK_ENTRY_DY, "OCR"
                self.log("warn", "⚠ 丝带模板未命中, 改用 OCR 定位入口 (可能游戏改版)")
            else:
                ex, ey = rk.RANK_ENTRY_HOTSPOT
                src = "兜底热区"
                self.log("warn", "⚠ 模板+OCR 均未定位到入口, 回退固定热区")
        self._tap(ex, ey)
        self.log("info", f"✓ 点击 荣耀排位赛 入口[{src}] @ ({ex},{ey})")

    def _is_city(self, img: np.ndarray) -> bool:
        """是否在主城 (有王者之巅建筑文字)."""
        return self._find_hint(img, ("王者之巅",), scale=1.0) is not None

    def _enter_pvp_hub(self, mode: str) -> bool:
        """从任何位置导航到对应 PvP 模式主页: 智能识别当前所在页 (主页/二级菜单/主城) 再走最短路径.
        mode='rank': 进入荣耀排位赛主页 (严格主页判定).
        mode='arena': 进入全民争霸列表页 (挑战列表就绪).
        失败存 anomaly 截图并返回 False.
        rank 模式入口 2026-09-13 起用丝带模板动态定位 (_tap_rank_entry),
        旧硬编码 RANK_ENTRY_HOTSPOT 仅作三级降级的最后兜底."""
        img = self._shot_img()
        if img is None:
            self.errors += 1
            return False

        # 0) 会话失效弹窗 / 停在登录页 -> 先重新登录再导航 (2026-09-13).
        #    必须放在**战斗页判定之前**: token 失效弹窗是半透明遮罩, 其下战斗页的
        #    「跳过/X2」仍可见 -> 否则会被判成"已在战斗页"而跳过登录, 卡死在结算环节。
        #    用 _session_nav_guard 防止 _recover_from_session -> _enter_pvp_hub_retry -> 本函数 的递归。
        if not getattr(self, "_session_nav_guard", False) and (
                self._is_session_expired(img) or self._is_login_screen(img)):
            self._session_nav_guard = True
            try:
                self.log("warn", "导航前检测到会话失效/停在登录页, 先重新登录")
                if self._recover_from_session("nav_entry") != "recovered":
                    return False
            finally:
                self._session_nav_guard = False
            img = self._shot_img()
            if img is None:
                self.errors += 1
                return False

        target_btn = "荣耀排位赛" if mode == "rank" else "全民争霸"

        # 0) 已在战斗页 -> 视为"已在流程内", 导航成功, 交主循环接管本局 (P0 根因修复 2026-09-12)
        #    根因: 下方三条分支只认 主页/二级菜单/主城, **没有战斗页分支**; 一旦在战斗中
        #    触发重导航 (冷却等待/被弹回主城/防挂机踢回 等自愈路径), 三分支全部落空 ->
        #    else -> "导航失败: 当前页面非主页/二级菜单/主城" + unknown_screen + 返回 False,
        #    任务当场终止. 配合 _enter_pvp_hub_retry(tries=3) 一次异常产出 3 张同画面截图.
        #    铁证 (09-12 07:5x): anomaly_075755/075821/075847_unknown_screen.png 三张全是
        #    标准战斗页 (跳过按钮可见, 模板匹配 0.98), 而 _classify_page 早已能正确返回
        #    'battle' —— 能力存在但本函数没用上, 属实现遗漏而非识别能力不足.
        #    判据用统一方法 _is_in_battle (「跳过」+「X2」双锚点模板 + OCR 兜底):
        #    单用「跳过」模板既不特异 (擂台页「创建擂台」按钮 0.718 会误命中, 进而
        #    对非战斗页乱点跳过坐标) 也不充分 (战斗页光效帧跌到 0.63 漏判), 故必须双锚点.
        if self._is_in_battle(img):
            self._nav_page = "battle"
            self.log("info", "导航: 当前已在战斗页 (跳过可见), 不打断本局, 交主循环跳过并结算")
            return True

        # 1) 已在目标页 -> 返回
        if mode == "rank" and self._is_rank_hub(img):
            # 2026-09-13 提速: 导航终点已断言在主页 -> 标记"下一轮起点可信", 让主循环
            # 直接走稳态快车道 (_try_fast_hub), 省掉 _detect_settle_stage + _is_in_battle
            # + _check_daily_limit 三项 (实测合计 ~15s OCR)。这正是"点单人匹配迟到"的
            # 余量来源: 首局拿不到"上一局结算链"留下的标志, 只能跑完整链 —— 在负载高的
            # 机器上全链 8~10 次 OCR 要 60~80s, 而倒计时只有几十秒, 必然归零被踢回主城。
            # 导航刚落点本身就是可信来源 (只可能是主页/二级菜单/主城/战斗页, 已分类过),
            # 不存在"落在上一局残留宝箱页"的风险, 故可安全置位。
            self._at_hub_confident = True
            return True
        if mode == "arena" and self._is_on_list(img):
            return True

        # 2) 在二级菜单 -> 点对应模式按钮 (rank 用丝带模板动态定位, 见 _tap_rank_entry)
        if self._is_pvp_submenu(img):
            self.log("info", f"导航: 已在二级菜单, 点 {target_btn}")
            if mode == "rank":
                self._tap_rank_entry(img)   # img 即当前帧, 模板/OCR 同帧缓存免费
            else:
                if not self._tap_hint((target_btn,), scale=1.0, label=target_btn):
                    self._save_anomaly(f"no_{target_btn}_btn"); return False
            self._sleep_stop(3)

        # 3) 在主城 -> 点王者之巅 (直接点四个大字本身) -> 等二级菜单 -> 点对应按钮
        #    判据统一 (2026-09-13 18:0x 实锤, 荣耀任务 nav_failed 三连根因①):
        #    原分支只认 `_is_city` (「王者之巅」单文字), 与 _classify_page 的主城判据
        #    (_is_in_main_city: tab 条带快路径 + 宽词表) 不一致 —— 一帧王者之巅 OCR 丢失
        #    而 tab 条带命中, 就掉进 else 分支打出矛盾日志 "(分类=main_city)", 且随后
        #    逃生按 back 在主城弹退出确认框, 连按把游戏按退出到桌面。
        #    现与分类器同源; 锚点丢失时重截一帧再找 (艺术字 OCR 偶发丢字), 仍无则留图
        #    收口 —— 宁可失败重试, 也不对主城按返回键。
        elif self._is_city(img) or self._is_in_main_city(img):
            wz = self._find_hint(img, ("王者之巅",), scale=1.0)
            if wz is None:
                self.log("warn", "主城判定成立但『王者之巅』文字未找到, 重截一帧再试")
                self._sleep_stop(1.5)
                img2 = self._shot_img()
                if img2 is not None:
                    img = img2
                    wz = self._find_hint(img, ("王者之巅",), scale=1.0)
            if wz is None:
                self._nav_page = "main_city"
                self.log("warn", "主城判定成立但两帧均未找到王者之巅锚点, 无法点入口 (留图收口)")
                self._save_anomaly("no_wangzhe_anchor")
                return False
            # 用户指引: "王者之巅"这四个字本身就是热区, 直接点文字中心,
            # 不要套用 hitbox 偏移 (88,-32) — 该公式会让点击偏离真实锚点.
            bx, by = int(wz["cx"]), int(wz["cy"])
            self.log("info", f"导航: 点王者之巅文字 @ ({bx},{by})")
            self._tap(bx, by)
            self._sleep_stop(2.5)
            if not self._wait_hint(("王者之争",), 10, scale=1.0, desc="等待二级菜单"):
                self.log("warn", "导航失败: 进入王者之巅后未出现二级菜单")
                self._save_anomaly("no_pvp_submenu")
                return False
            if mode == "rank":
                img2 = self._shot_img()   # 画面已切到二级菜单, 旧 img 还是主城帧
                if img2 is None:
                    self.errors += 1
                    return False
                self._tap_rank_entry(img2)
            else:
                if not self._tap_hint((target_btn,), scale=1.0, label=target_btn):
                    self._save_anomaly(f"no_{target_btn}_btn"); return False
            self._sleep_stop(3)
        else:
            # 真的落在未知页: 再跑一次通用分类器, 把结论写进日志 (只在此失败路径执行,
            # 约 6-7s 的多次 OCR 开销不会影响正常导航). 下次再出问题可从日志直接看出
            # 分类器认成了什么, 不必靠人工看图.
            page = self._classify_page(img)
            self._nav_page = page
            # 通用逃生 (2026-09-12): 未登记页面 (副本关卡/排行榜/活动页等) 先连按系统
            # 返回键退回已知页再重试, 而不是立刻判导航失败 -> 任务终止.
            # 只做一次完整逃生 (由 _nav_back_tried 保证), 且本分支已排除战斗页
            # (步骤 0 接管), 故不会误触"退出战斗"确认框.
            if not self._nav_back_tried:
                self._nav_back_tried = True
                self.log("warn", f"导航遇到未登记页面 (分类={page}), 按返回键逃生后重试")
                if self._escape_unknown_page("nav") is not None:
                    return self._enter_pvp_hub(mode)   # 递归重试一次 (受 _nav_back_tried 限制)
            self.log("warn", f"导航失败: 当前页面非主页/二级菜单/主城/战斗页"
                             f" (分类器判定={page}), 逃生后仍无法识别")
            self._save_anomaly("unknown_screen")
            return False

        # 4) 最终断言进入目标页
        img = self._shot_img()
        if img is None:
            self.errors += 1
            return False
        # P0 (2026-09-11 根因修复): 原断言只认"主页"(_is_rank_hub), 但点击荣耀排位赛
        # 入口后游戏若已进入匹配/战斗 (勾了"自动参与", 或点击恰好触发匹配), 截图即战斗页,
        # 被误判为导航失败 -> 任务当场终止 (09-11 实锤: anomaly_094917_rank_hub_failed.png
        # 里是标准战斗页, 有跳过/X2/血条, 却报"未进入主页").
        # 改为复用 _classify_page: hub/battle/reward/end/back 均属"已在排位流程内",
        # 视为导航成功; 落点写入 self._nav_page 供主循环决定"直接接管本局"还是"走匹配".
        if mode == "rank":
            _t_cls = time.time()
            page = self._classify_page(img)
            _dt_cls = time.time() - _t_cls
            self._nav_page = page
            if _dt_cls >= 5.0:
                # 落点判定是导航的第二大耗时项 (首位是起步前的分支判定). 超过 5s 才打日志,
                # 避免刷屏; 需要精确定位时看这一行 + _enter_pvp_hub_retry 的总耗时即可.
                self.log("info", f"[耗时] 落点判定 = {_dt_cls:.1f}s (页面={page})")
            if page in ("hub", "battle", "reward", "end", "back"):
                if page == "hub":
                    # 落点已由 _classify_page 断言为主页 -> 下一轮起点可信, 开稳态快车道
                    self._at_hub_confident = True
                    self.log("info", "导航完成: 已进入荣耀排位赛主页")
                else:
                    self.log("info", f"导航落点: {page} (已在排位流程内, 交主循环接管)")
                return True
            self.log("warn", f"导航失败: 点荣耀排位赛后页面仍为 {page} (非排位流程)")
            self._save_anomaly("rank_hub_failed")
            return False
        if mode == "arena" and not self._is_on_list(img):
            self.log("warn", "导航失败: 点全民霸后未进入列表页")
            self._save_anomaly("arena_list_failed")
            return False
        return True

    def _enter_pvp_hub_retry(self, mode: str, tries: int = 3) -> bool:
        """导航重试包装 (P1, 2026-09-11 根因修复).

        背景: _enter_pvp_hub 单次执行, 任一步失败即 return False; 全部调用点均为
        "失败即收尾". 全项目只有匹配等待期有"多试几次"(main_city_rescues/match_rescues),
        导航本身零重试 -> 09-11 实例: 设 30 场, 导航失败一次即 fought=0 收工.

        行为: 最多 tries 次, 每次失败后等 2.5s 重走导航链 (第二次通常已加载完成,
        直接命中"已在目标页"分支立即成功). 停止请求可随时中断.
        返回 True 时 self._nav_page 记录落点 (hub/battle/reward/...), 主循环据此接管."""
        # 每轮重试序列允许一次"返回键逃生" (未登记页面 -> 按 back 退回再试)
        self._nav_back_tried = False
        for i in range(1, tries + 1):
            if self.stop_evt.is_set():
                return False
            # 2026-09-13 18:0x 实锤 (荣耀任务 nav_failed 三连): 首轮逃生可能把游戏按退出
            # 到模拟器桌面 (主城 back -> 退出确认框), 或任务起步前游戏已被回收 ——
            # 后续重试若不查, 会在桌面上盲跑 35s 全链 OCR 直到 3 连败收尾 (18:03~18:05
            # 实录, 重试 2/3、3/3 全程 classify=unknown 无人拉起游戏)。
            # 每轮尝试前确认游戏在前台; gone/background 即拉起 (登录页/会话弹窗由
            # _enter_pvp_hub 步骤 0 接手)。探测失败=unknown 不轻举妄动, 交页面判据兜底。
            _st = self._check_game_health()
            if _st in ("gone", "background"):
                self.log("warn", f"导航第 {i} 次尝试前游戏不在前台 ({_st}), 先拉起游戏")
                if not self._ensure_game_foreground(f"nav_try{i}"):
                    self.log("error", "拉起游戏失败, 导航中止")
                    return False
            # 2026-09-13 可观测性: 导航是"被踢回主城后重跑"的关键路径, 其耗时构成
            # 此前只能靠人工估 (实测 ~88s = 起步判定 + 点击 + 落点断言). 这里把每次
            # 尝试的总耗时写进日志, 下次变慢/变快都能直接从日志看出来, 不必再逐条计时.
            _t0 = time.time()
            _ok = self._enter_pvp_hub(mode)
            _dt = time.time() - _t0
            if _ok:
                if i > 1:
                    self.log("ok", f"✓ 导航第 {i}/{tries} 次尝试成功")
                self.log("info", f"[耗时] 本轮导航 {_dt:.1f}s "
                                 f"(落点={getattr(self, '_nav_page', None)})")
                return True
            self.log("warn", f"导航第 {i}/{tries} 次未成功, 耗时 {_dt:.1f}s")
            if i < tries:
                self.log("warn", f"导航未成功, 2.5s 后重试 ({i}/{tries})")
                self._sleep_stop(2.5)
        self.log("error", f"导航连续 {tries} 次失败, 放弃并收尾")
        return False

    def _wait_hint(self, hints, timeout, scale=1.5, desc="", roi=None):
        """轮询 OCR 直到命中 hints 中任一文字, 返回首个命中块或 None。
        roi 指定时只在裁剪区 OCR (速度优化: 结算/按钮文字位置固定, 小 ROI 一次 40-200ms,
        全图一次 ~3.5s). 命中帧缓存在 self.last_img, 调用方可直接复用判胜负."""
        def f():
            img = self._shot_img()
            if img is None:
                return None
            return self._find_hint(img, hints, roi=roi, scale=scale)
        label = desc or f"等待 {hints[0]}"
        return self._wait_until(f, timeout, 1.0, label)

    def _tap_hint(self, hints, scale=1.5, label=""):
        """OCR 全图找 hints, 取最佳命中点点击。返回是否点中。"""
        img = self._shot_img()
        if img is None:
            return False
        hit = self._find_hint(img, hints, scale=scale)
        if not hit:
            return False
        self._tap(hit["cx"], hit["cy"])
        if label:
            self.log("info", f"✓ 点击 {label}")
        return True

    def _step_single_match(self, img: Optional[np.ndarray] = None) -> bool:
        """主页匹配按钮: 若显示'单人匹配'则点击; 若显示'取消匹配'则跳过点击
        (按钮固定命名 / 已在匹配中) 直接进入等待比赛界面.
        注: 实测 OCR 主页按钮文字为'取消匹配'而非'单人匹配', 用 OCR 文字区分避免误触取消.
        img 可复用调用方已截的图, 省一次截图 (~1.5s)."""
        if img is None:
            img = self._shot_img()
        if img is None:
            self.errors += 1
            return False
        sm = self._find_hint(img, ("单人匹配",), roi=rk.HUB_BTN_ROI, scale=1.0)
        if sm:
            self._tap(sm["cx"], sm["cy"])
            _t0 = getattr(self, "_round_t0", None)
            _extra = ""
            if _t0:
                _extra = f" [起步→点匹配 {time.time() - _t0:.1f}s]"
                self._round_t0 = None
            self.log("info", f"✓ 点击 单人匹配 (开始匹配){_extra}")
            return True
        cancel = self._find_hint(img, ("取消匹配",), roi=rk.HUB_BTN_ROI, scale=1.0)
        if cancel:
            self.log("info", "按钮显示'取消匹配' (按钮固定命名/已在匹配), 不重复点击, 进入等待比赛界面")
            return True
        # 盲点回退坐标前, 先确认**确实在排位主页** (2026-09-12 根因防护).
        # 盲点 (633,500) 只要落在别的主城/活动页上, 就会点到未知入口
        # (用户反馈"有时会莫名进到地下城里的副本关卡"). 宁可这局不点:
        # 不点 -> _wait_battle_ui 超时会走页面识别 + 返回键逃生自愈, 不会乱点.
        if not self._is_rank_hub(img, fast=True) and not self._is_rank_hub(img):
            self.log("warn", "OCR 未识别匹配按钮文字, 且当前不在排位主页 -> 不盲点坐标"
                             " (交等待/逃生自愈处理)")
            return True
        self.log("warn", "OCR 未识别匹配按钮文字, 已在排位主页 -> 用回退坐标点单人匹配槽位")
        self._tap(*rk.SINGLE_MATCH_FALLBACK)
        return True

    def _check_rank_cooldown(self, img: Optional[np.ndarray] = None) -> bool:
        """单次检测当前画面是否为冷却弹窗 ("你今日已挑战10次,请休息15分钟").
        命中: 点确认关窗, 返回 True (调用方应停止本轮).
        与旧版不同: 不再固定空转 COOLDOWN_CHECK_SEC, 而是由 _wait_battle_ui 等待期
        每次轮询顺带检测 (冷却弹窗一定出现在点击匹配后到开赛之间, 无冷却时零开销)."""
        if img is None:
            img = self._shot_img()
        if img is None:
            return False
        if self._find_hint(img, rk.COOLDOWN_HINTS, roi=rk.COOLDOWN_ROI, scale=1.0) is not None:
            self.log("warn", "检测到冷却弹窗: 今日已挑战 10 次")
            self._save_stage("cooldown_popup")
            fx, fy = rk.COOLDOWN_CONFIRM_FALLBACK
            self._tap(fx, fy)
            self.log("info", "✓ 点击冷却弹窗确认")
            return True
        return False

    def _check_daily_limit(self, img: Optional[np.ndarray] = None) -> bool:
        """单次检测 "今日已累计挑战30次, 请明日再来" 弹窗 (每日 30 次上限, 2026-09-09 实测).
        与 10 把冷却本质不同: 当日无法等待解除 (18-20 荣耀窗口除外) -> 命中即应终止任务
        并明确告知用户. 弹窗仅提示文字无确认按钮 (截图 18:08), 不点击任何坐标."""
        if img is None:
            img = self._shot_img()
        if img is None:
            return False
        if self._find_hint(img, rk.DAILY_LIMIT_HINTS, roi=rk.DAILY_LIMIT_ROI, scale=1.0) is not None:
            self.log("warn", "检测到每日上限弹窗: 今日已累计挑战 30 次, 请明日再来"
                             " (其中 18-20 点荣耀时刻不受场次限制)")
            self._save_stage("daily_limit")
            return True
        return False

    def _is_pve_stage(self, img: Optional[np.ndarray] = None) -> bool:
        """是否在 PvE 副本关卡页 (地下城章节图, 引擎绝不该在的页面).

        2026-09-12 实测 (anomaly_131731/134647_no_battle_ui.png): 画面 = 章节名
        「宏伟屏障」+ 关卡节点 20-1~20-10 + 左下「星数奖励」+ 右下星数 (427/40) + 「BOSS」.
        该页所有既有判据都不认 -> _classify_page 返回 unknown -> 等比赛界面 200s 超时
        后报 no_battle_ui 终止, 且引擎没有退出手段 (用户反馈: "根本就不知道怎么退出").

        成本: 2 次小 ROI OCR (~0.3-0.6s each), 只在"其它判据全不认"的慢路径上调用.
        """
        if img is None:
            img = self._shot_img()
        if img is None:
            return False
        return (self._find_hint(img, rk.PVE_STAGE_HINTS, roi=rk.PVE_TITLE_ROI, scale=1.0)
                is not None
                or self._find_hint(img, rk.PVE_STAGE_HINTS, roi=rk.PVE_STAR_ROI, scale=1.0)
                is not None)

    def _is_session_expired(self, img: Optional[np.ndarray] = None) -> bool:
        """是否弹出会话失效弹窗 (「token失效，请重新登录」+「确认」)。

        2026-09-13 实测根因: 这是**半透明模态遮罩**, 战斗画面的「跳过」「X2」在遮罩下
        依然可见 ⇒ `_is_in_battle` 仍为真。引擎原先没有这一档, 于是结算环节把它认成
        "battle" -> 反复"重新点跳过" (点击全被遮罩吃掉) -> 自愈 4 次超限 -> 任务终止。
        成本: 1 次小 ROI OCR (400x65, 实测 ~0.5-1.5s)。
        """
        if img is None:
            img = self._shot_img()
        if img is None:
            return False
        return self._find_hint(img, rk.SESSION_HINTS,
                               roi=rk.SESSION_TEXT_ROI, scale=1.0) is not None

    def _is_login_screen(self, img: Optional[np.ndarray] = None) -> bool:
        """是否停在登录页 (大「开始」+ 底部 账号登录/选择服务器/选择角色)。

        按**关键词命中数**判定 (≥ LOGIN_MIN_HITS) 而不是块数 —— 实测该区域 ROI 一旦偏扁,
        det 会把三行并成一个乱码块 (如 "照号登滋深服务器滋择角色"), 用块数会漏判;
        用关键词计数则乱码块里的 "服务器"/"角色" 仍能命中, 鲁棒得多。
        成本: 1 次 ROI OCR (~1.5-2.5s, 只在自愈/超时等冷路径调用)。
        """
        if img is None:
            img = self._shot_img()
        if img is None:
            return False
        blob = " ".join(it["text"] for it in
                        self._ocr_texts(img, roi=rk.LOGIN_HINT_ROI, scale=1.0))
        if not blob:
            return False
        hits = sum(1 for h in rk.LOGIN_HINTS if h in blob)
        return hits >= rk.LOGIN_MIN_HITS

    # ================================================================ 窗口内自愈闸门
    def _mode_window(self) -> Optional[str]:
        """本任务的时段闸门类型: 'rank_glory' / 'arena' / None(无窗口概念)。

        只有日常排位(rank)能在任意时刻打, 其余两种都受时段约束 —— 这正是"窗口内就该
        坚持进排位、窗口外才该收尾"这条新策略的判定依据。
        """
        mode = getattr(self, "mode", None)
        if mode == "rank_glory":
            return "rank_glory"
        if mode == "arena":
            return "arena"
        return None

    def _selfheal_gate(self, streak: int) -> str:
        """自愈还能不能再来一次。返回 'go' / 'closed' / 'stall' / 'cap'。

        'closed' = 已不在该模式的时段内 (再重试也没有比赛可打) -> 立刻收尾, 不空转;
        'stall'  = 窗口内但连续 SELFHEAL_STALL_SEC 没能打成一局 -> 判真故障收尾;
        'cap'    = 无窗口模式(rank) 的旧次数上限 (语义保持, 避免影响日常排位的行为);
        'go'     = 继续自愈。
        """
        jt = self._mode_window()
        if jt is None:
            return "cap" if streak > SELFHEAL_LEGACY_MAX else "go"
        from sj_bot.state_machine import in_glory_window, in_window
        open_now = in_glory_window() if jt == "rank_glory" else in_window()
        if not open_now:
            return "closed"
        since = getattr(self, "_selfheal_since", None)
        if since is None:
            self._selfheal_since = time.time()
        elif time.time() - since > SELFHEAL_STALL_SEC:
            return "stall"
        return "go"

    def _selfheal_ok(self, family: str, label: str) -> bool:
        """记一次该族自愈并过闸门。True=可以继续自愈; False=该收尾(原因已写 `_last_stop_kind`)。

        把"游戏不在排位"的五族自愈 (nav/session/game/main_city/match) 统一到窗口语义:
        窗口内可持续重导航进排位, 窗口关闭或长时间无进展才终止。
        """
        streak = getattr(self, family, 0) + 1
        setattr(self, family, streak)
        verdict = self._selfheal_gate(streak)
        if verdict == "go":
            # 每次闸门判定都刷新, 保证调用方读到的 `_last_stop_kind` 不会残留上一条
            self._last_stop_kind = ""
            return True
        if verdict == "closed":
            self.log("error", f"{label}: 已不在任务时段内, 不再重试, 任务收尾 (第 {streak} 次触发)")
            self._last_stop_kind = "window_closed"
        elif verdict == "stall":
            self.log("error",
                     f"{label}: 窗口内连续 {SELFHEAL_STALL_SEC // 60} 分钟未能打成一局 "
                     f"(自愈已 {streak} 次), 判定真故障, 任务终止 (现场图见 captures/)")
            self._last_stop_kind = "selfheal_stall"
        else:
            self.log("error", f"{label}: 自愈已达上限 (>{SELFHEAL_LEGACY_MAX}), 任务终止")
            self._last_stop_kind = "selfheal_cap"
        return False

    def _selfheal_pause(self, streak: int) -> None:
        """自愈退避: 次数不再设硬上限后, 用它防止变成热循环刷屏。"""
        if streak <= 0:
            return
        sec = SELFHEAL_BACKOFF[min(streak - 1, len(SELFHEAL_BACKOFF) - 1)]
        self.log("info", f"窗口内自愈退避 {sec}s 后继续重试 (第 {streak} 次)")
        self._sleep_stop(sec)

    def _selfheal_progress(self) -> None:
        """本局已确认进入战斗页/结算链 = 有进展 -> 重置"窗口内无进展"计时。"""
        self._selfheal_since = None

    def _stop_reason(self, fallback: str) -> str:
        """收尾原因: 自愈闸门给了精确原因(window_closed/selfheal_stall/selfheal_cap)就用它,
        否则用调用方原语义的兜底代码 —— 这样页面"上次结果"能直接说明**为什么**停。"""
        return getattr(self, "_last_stop_kind", "") or fallback

    def _recover_from_session(self, tag: str = "") -> str:
        """会话失效 / 登录页自愈: 关弹窗 -> 点「开始」重新登录 -> 重导航回排位主页。

        返回 'recovered' (调用方可 continue 重试本局) / 'stop' (该收尾, 精确原因写在
        `self._last_stop_kind`) / 'nav_fail'。
        2026-09-22 变更: 不再用"固定 3 次上限"。窗口还开着就持续重登录+重导航回排位
        (见 `_selfheal_gate`); 窗口关了或窗口内长时间无进展才 stop。
        """
        if not self._selfheal_ok("session_rescues", "会话失效/停在登录页"):
            return "stop"
        self._selfheal_pause(self.session_rescues)
        self.log("warn", f"检测到游戏会话失效/停在登录页 ({tag or '未知入口'}), "
                         f"尝试重新登录并重导航 (第 {self.session_rescues} 次)")
        self._save_anomaly(f"session_{tag or 'x'}")
        img = self._shot_img()
        if img is not None and self._is_session_expired(img):
            self.log("warn", "点「确认」关闭 token 失效弹窗 (确认后游戏通常回登录页)")
            self._tap(*rk.SESSION_CONFIRM_COORD)
            self._sleep_stop(3)
        if self._is_login_screen():
            self.log("warn", "检测到登录页, 点「开始」重新登录")
            self._tap(*rk.LOGIN_START_COORD)
        # 等回到主城/主页: 覆盖 登录加载 + 开场公告 splash (实测 splash 需点掉/自动收)
        deadline = time.time() + 45
        arrived = False
        nudges = 0
        while not self.stop_evt.is_set() and time.time() < deadline:
            self._sleep_stop(3)
            img = self._shot_img()
            if img is None:
                continue
            if self._is_in_main_city(img) or self._is_rank_hub(img, fast=True):
                arrived = True
                break
            if self._is_session_expired(img):
                self._tap(*rk.SESSION_CONFIRM_COORD)
                continue
            if self._is_login_screen(img):
                self._tap(*rk.LOGIN_START_COORD)
                continue
            # 既不认识也没到主城 —— 多半是开场公告 splash, 用返回键收掉 (最多 2 次)
            nudges += 1
            if nudges <= 2 and time.time() - (deadline - 45) > 12:
                self.log("warn", "重新登录后停在未识别页面 (疑开场公告), 按返回键收掉")
                self._press_back()
        if self.stop_evt.is_set():
            return "stop"
        if not arrived:
            self.log("warn", "重新登录后 45s 内未回到主城/主页, 直接尝试导航 (交导航自愈处理)")
        if not self._enter_pvp_hub_retry("rank"):
            return "nav_fail"
        return "recovered"

    def _classify_page(self, img: Optional[np.ndarray] = None) -> str:
        """超时自愈用: 单张截图识别当前处于哪个已知页面.
        返回: 'session' (会话失效弹窗, 最先判) / 'daily_limit' / 'cooldown' / 'reward' / 'end' /
              'back' / 'battle' / 'hub' (排位主页) / 'main_city' / 'login' (登录页) /
              'pve_stage' (副本关卡页) / 'unknown'.
        判定顺序: 会话失效弹窗 -> 终止类弹窗 -> 结算链各页 (reward/end/back) -> 战斗页 -> 主页
                  -> 主城 -> 登录页 -> 副本关卡页 (最后, 因为它只用于"逃生", 不参与正常流程).
        全部复用既有检测器 (模板/ROI OCR), 单次 ~1-2s."""
        if img is None:
            img = self._shot_img()
        if img is None:
            return "unknown"
        # ★ 会话失效弹窗必须**最先**判 (2026-09-13 实测根因): 它是覆盖一切的半透明模态层,
        #   其下战斗页的「跳过/X2」依然可见 -> 若排在战斗页之后, 就永远判不出来。
        if self._is_session_expired(img):
            return "session"
        # ⚠️ 2026-09-13 P1b 复盘: 曾把「主城 tab 条带」提到这里短路 (想让主城帧省掉后面
        #   33s 的逐条 OCR)。**实测后已回退** —— 因为本函数的调用场景以**主页帧为主**
        #   (导航落点断言 / 首局完整链 / 结算自愈 / 匹配超时自愈), 而 tab 条带在主页上的
        #   成本并不便宜: 实测 (60,700,1560,900) 在主页帧要 10.2~22.1s (下区内容密集),
        #   收窄 x 到 (210,690,800,900) 仍要 3.9~11.8s。主城帧只省 ~8s, 主页帧却多花
        #   4~22s ⇒ **净负**。结论: 要提速必须让"主城检查"在非主城帧上**近乎免费**,
        #   唯一办法是加一步**模板预筛** (tab 图标模板, cv2 约 0.1s), 属 P2 待办;
        #   在模板就位前, 顺序不得改动。见 MEMORY.md「待办 P2」。
        if self._find_hint(img, rk.DAILY_LIMIT_HINTS, roi=rk.DAILY_LIMIT_ROI, scale=1.0):
            return "daily_limit"
        if self._find_hint(img, rk.COOLDOWN_HINTS, roi=rk.COOLDOWN_ROI, scale=1.0):
            return "cooldown"
        if self._hint_or_tpl(img, "reward_pick_text.png", rk.REWARD_HINTS,
                             rk.REWARD_ROI, rk.REWARD_TPL_HI, rk.REWARD_TPL_LO):
            return "reward"
        if self._hint_or_tpl(img, "end_btn.png", rk.END_HINTS, rk.END_ROI,
                             rk.END_TPL_HI, rk.END_TPL_LO):
            return "end"
        if self._hint_or_tpl(img, "back_btn.png", rk.BACK_HINTS, rk.BACK_ROI,
                             rk.BACK_TPL_HI, rk.BACK_TPL_LO):
            return "back"
        if self._is_in_battle(img):
            return "battle"
        if self._is_rank_hub(img):
            return "hub"
        if self._is_in_main_city(img):
            return "main_city"
        if self._is_login_screen(img):
            return "login"
        if self._is_pve_stage(img):
            return "pve_stage"
        return "unknown"

    def _detect_settle_stage(self, img: Optional[np.ndarray] = None) -> Optional[str]:
        """当前是否停在"结算链"某页 (宝箱 / 比赛结束 / 返回大厅).

        用途 (2026-09-12): 冷却续打、被弹回主城后的重导航, 常常恰好落在宝箱结算页.
        而结算页背景仍是排位界面 —— 顶部有"荣耀排位赛"标题、页面上有"下次比赛时间"
        等主页独有元素, 于是被 _is_rank_hub 判成主页; 主循环照常去点"单人匹配",
        白等 200s 后被系统踢回主城. 09-12 日志实证:
          11:08:50 导航落点: reward -> 11:09:22 ✓ 点击 单人匹配 -> 11:11:40 被弹回主城.
        识别出来后把结算流程的入口 stage 直接设成该页, 即可原地接管继续推进.

        返回 'reward' / 'end' / 'back' / None (顺序与 _classify_page 一致, 宝箱优先).
        成本 (P2 门控后): 1 次 ROI OCR (~1s, 宝箱文案) + 2 次模板匹配 (~0.15s)。
        end/back 走三值门控: 稳态帧 (不在这两页) 实测 0.31 / 0.19, 均 <= lo ⇒ **直接否决,
        不再跑 OCR**; 只有在中间区间 (异常帧) 才退回 OCR。见 rk.END_TPL_HI 注释。
        """
        if img is None:
            img = self._shot_img()
        if img is None:
            return None
        if self._hint_or_tpl(img, "reward_pick_text.png", rk.REWARD_HINTS,
                             rk.REWARD_ROI, rk.REWARD_TPL_HI, rk.REWARD_TPL_LO):
            return "reward"
        if self._hint_or_tpl(img, "end_btn.png", rk.END_HINTS, rk.END_ROI,
                             rk.END_TPL_HI, rk.END_TPL_LO):
            return "end"
        if self._hint_or_tpl(img, "back_btn.png", rk.BACK_HINTS, rk.BACK_ROI,
                             rk.BACK_TPL_HI, rk.BACK_TPL_LO):
            return "back"
        return None

    def _wait_out_cooldown(self) -> str:
        """每日 10 把冷却的"等待后续打"处理 (2026-09-09 用户需求):
        不再终止任务, 停止可打断地等满 15 分钟后重导航回排位主页继续剩余场数.
        返回: 'ready' 已就绪可继续 / 'stopped' 手动停止 / 'nav_fail' 重导航失败."""
        self.cooldown_waits += 1
        total = rk.COOLDOWN_WAIT_TOTAL_SEC
        self._progress("cooldown", f"每日 10 把冷却, 等待 {total // 60} 分钟后自动继续 (第 {self.cooldown_waits} 次)")
        self.log("warn", f"今日已打满 10 把, 等待 15 分钟后自动继续剩余场数 (第 {self.cooldown_waits} 次)")
        waited = 0
        while waited < total:
            if self.stop_evt.is_set():
                return "stopped"
            step = min(30, total - waited)
            self._sleep_stop(step)
            if self.stop_evt.is_set():
                return "stopped"
            waited += step
            remain = total - waited
            if remain > 0:
                self._progress("cooldown", f"冷却等待中, 剩余 {remain // 60} 分 {remain % 60:02d} 秒")
        self.log("info", "冷却结束, 重新导航回排位主页继续")
        if not self._enter_pvp_hub_retry("rank"):
            self.log("error", "冷却结束后重导航失败")
            self._save_anomaly("cooldown_nav_fail")
            return "nav_fail"
        return "ready"

    def _antibot_lines(self, img: np.ndarray) -> list[list[dict]]:
        """把弹窗 ROI 内 OCR 文字块按 y 聚类为行 (行内按 x 排序)。
        返回 [[{text,cx,cy},...], ...] 自上而下; 用于题干/选项结构分析."""
        texts = self._ocr_texts(img, roi=rk.ANTIBOT_ROI, scale=1.0)
        texts.sort(key=lambda it: it["cy"])
        rows: list[list[dict]] = []
        for t in texts:
            if rows and abs(t["cy"] - rows[-1][0]["cy"]) < rk.ANTIBOT_LINE_TOL:
                rows[-1].append(t)
            else:
                rows.append([t])
        for r in rows:
            r.sort(key=lambda it: it["cx"])
        return rows

    def _antibot_ocr_items(self, img: Optional[np.ndarray] = None) -> list[dict]:
        """裁弹窗 ROI + 双 OCR 路径 (scale2.0 彩色 / 灰度二值), 返回全部文字项.
        坐标均为 **弹窗 ROI 内坐标** (cx/cy 除以 scale, 未加 ROI 起点).
        经验: 弹窗文字带光晕, 全图 OCR 经常漏字; 小 ROI 放大后命中率高.
        供 _check_antibot 查触发词 & _solve_antibot 本地算术解题共用, 避免重复 OCR."""
        if img is None:
            img = self._shot_img()
        if img is None:
            return []
        x0, y0, x1, y1 = rk.ANTIBOT_ROI
        crop = img[y0:y1, x0:x1]
        if crop is None or crop.size == 0:
            return []
        import cv2 as _cv
        items: list[dict] = []
        seen: set[tuple] = set()
        for scale, prep in [(2.0, None), (1.0, "gray")]:
            if prep == "gray":
                work = _cv.cvtColor(crop, _cv.COLOR_BGR2GRAY)
                _, work = _cv.threshold(work, 170, 255, _cv.THRESH_BINARY)
            else:
                work = _cv.resize(crop, None, fx=scale, fy=scale,
                                  interpolation=_cv.INTER_CUBIC) if scale != 1.0 else crop
            try:
                result, _ = self.ocr(work)
            except Exception:
                continue
            if not result:
                continue
            for box, text, score in result:
                if not (isinstance(text, str) and text.strip()):
                    continue
                try:
                    sc = float(score)
                except (TypeError, ValueError):
                    sc = 0
                if sc < 0.4:
                    continue
                try:
                    pts = [(float(p[0]) / scale, float(p[1]) / scale) for p in box
                           if isinstance(p, (list, tuple)) and len(p) >= 2]
                except Exception:
                    continue
                if len(pts) < 4:
                    continue
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                key = (text.strip(), int(cx / 20), int(cy / 20))
                if key in seen:
                    continue  # 双路径同一文字块去重 (坐标取先到的那份)
                seen.add(key)
                items.append({"text": text.strip(), "cx": cx, "cy": cy})
        return items

    def _locate_antibot_options(self, img: Optional[np.ndarray],
                               n_opts: int) -> Optional[list[tuple[int, int]]]:
        """动态定位防挂机弹窗的选项按钮中心, 返回 **ANTIBOT_ROI 内相对坐标** 列表
        (与 rk.ANTIBOT_OPTION_COORDS 同语义, 调用方统一加 ROI 起点转全图绝对坐标).

        背景 (2026-09-13 根因修复): 旧实现把 4 个按钮坐标写死为 (200,259)...(全图 y=459),
        但实测弹窗选项行的 **y 中心会漂移** (448/457/478/519/612, 同一账号跨天跨次).
        位置一变, 点击就落在按钮上方空白 -> 弹窗不消失 -> 判"选错重出题" -> 4 次
        尝试全落空 -> giveup 终止任务. 10:48 实例: 选项行实测 y=489-549 (中心 519),
        代码点 y=459, 30 场只打了 10 场.

        做法: 选项按钮是弹窗内**唯一的高饱和青蓝胶囊条**, 不依赖 OCR (弹窗数字 OCR 不稳):
          ① 在 ANTIBOT_ROI 内做 BGR 掩码, 逐行列计数 -> 覆盖度最高的那一行 = 按钮行;
          ② 行内按列覆盖阈值取 x 跨度, 按 n_opts 等分 -> 各按钮中心.
        返回 None = 未定位成功 (调用方回退 rk.ANTIBOT_OPTION_COORDS 并记 warn).
        """
        if img is None or n_opts <= 0:
            return None
        x0, y0, x1, y1 = rk.ANTIBOT_ROI
        crop = img[y0:y1, x0:x1]
        if crop is None or crop.size == 0:
            return None
        b = crop[:, :, 0].astype(np.int16)
        g = crop[:, :, 1].astype(np.int16)
        r = crop[:, :, 2].astype(np.int16)
        mask = ((b > rk.ANTIBOT_BTN_MIN_B) & (g > rk.ANTIBOT_BTN_MIN_G)
                & (r < rk.ANTIBOT_BTN_MAX_R))
        rows = mask.sum(axis=1)
        if rows.size == 0 or int(rows.max()) <= 0:
            return None
        ys = np.where(rows >= rows.max() * 0.5)[0]
        if ys.size == 0:
            return None
        by0, by1 = int(ys.min()), int(ys.max())
        if by1 - by0 < rk.ANTIBOT_BTN_MIN_H:
            return None      # 太扁 -> 不是按钮行 (防背景青色噪点误判)
        band = mask[by0:by1 + 1]
        cols = band.sum(axis=0)
        xs = np.where(cols >= band.shape[0] * rk.ANTIBOT_BTN_COL_COVER)[0]
        if xs.size < n_opts:
            return None
        bx0, bx1 = int(xs.min()), int(xs.max())
        cy = (by0 + by1) // 2
        return [(bx0 + int((bx1 - bx0) * (k + 0.5) / n_opts), cy)
                for k in range(n_opts)]

    def _check_antibot(self, img: Optional[np.ndarray] = None) -> bool:
        """当前画面是否出现防挂机验证弹窗 (复用 _antibot_ocr_items 查触发词)."""
        for it in self._antibot_ocr_items(img):
            if any(k in it["text"] for k in rk.ANTIBOT_TRIGGERS):
                return True
        return False

    def _solve_antibot(self) -> str:
        """处理防挂机验证弹窗. **本地算术 + 视觉模型交叉作答** (2026-09-10 用户要求:
        模型每次都参与保证答对不被踢; 弹窗一天最多 ~3-4 次, 视觉调用成本可忽略):
          1) 等光球特效淡出 -> OCR 弹窗 ROI -> 本地解析算术题得候选按钮 (零成本);
          2) 视觉模型直读同一张图给按钮序号 (付费点, 受每日预算闸门管控);
          3) 决策: 模型有效答案优先; 本地答案交叉比对, 分歧记日志并信模型
             (视觉直读 > OCR 文本解析); 模型不可用/失败 -> 回退本地; 双无 -> 重试;
          4) 点击 -> 复查: 弹窗消失 -> 6s 后落在主城=kicked, 否则 solved.
        返回: 'solved' 通过 / 'kicked' 选错被踢回主城 (调用方重导航重试本局) /
              'giveup' 重试耗尽放弃 / 'retry' 截图失败 (调用方等同放弃)."""
        self.log("warn", "⚠️ 检测到防挂机验证弹窗, 本地算术 + 视觉模型交叉作答")
        self._save_anomaly("antibot_first")
        llm = None   # None=未尝试, False=确认不可用 (本弹窗内不再重试加载)
        n_opts = len(rk.ANTIBOT_OPTION_COORDS)
        for attempt in range(1, rk.ANTIBOT_MAX_TRIES + 1):
            self.log("info", f"防挂机答题尝试 {attempt}/{rk.ANTIBOT_MAX_TRIES}")
            # 弹窗出现初期有匹配光球特效, 每次都等其淡出再截图 (历史教训: 特效下 OCR 读错数字)
            self._sleep_stop(rk.ANTIBOT_RETRY_WAIT_SEC)
            img = self._shot_img()
            if img is None:
                return "retry"
            x0, y0, x1, y1 = rk.ANTIBOT_ROI
            crop = img[y0:y1, x0:x1]
            if crop is None or crop.size == 0:
                return "retry"

            # ---- 路径 A: 本地算术候选 (零成本, 兼作模型交叉验证参照) ----
            from sj_bot.antibot_solver import solve_math_option
            local_hit = solve_math_option(self._antibot_ocr_items(img))

            # ---- 路径 B: 视觉模型直读 (每次参与; 失败不阻塞, 由本地兜底) ----
            llm_idx = None
            if llm is None:
                try:
                    from sj_bot.llm_client import LLMClient
                    llm = LLMClient()
                except Exception as e:
                    llm = False
                    self.log("warn", f"视觉模型不可用 ({e}), 仅用本地算术作答")
            if llm is not False:
                try:
                    from sj_bot.llm_client import encode_cv_image_for_llm, get_budget
                    b64 = encode_cv_image_for_llm(crop, max_side=800)  # 压缩后发送省 token
                    llm_idx = llm.vision_antibot(b64, n_options=n_opts)
                    _bg = get_budget()
                    self.log("warn", f"⚠️ 视觉模型今日第 {_bg['calls']}/{_bg['cap']} 次付费调用")
                except Exception as e:
                    self.log("warn", f"视觉模型调用失败 ({e}), 回退本地答案")
                    llm_idx = None

            # ---- 决策: 模型优先, 本地交叉比对 ----
            # 选项按钮**动态定位** (2026-09-13 根因修复): 弹窗 y 会漂移, 硬编码坐标点空.
            dyn = self._locate_antibot_options(img, n_opts)
            if dyn:
                opts = dyn
                self.log("info", f"弹窗选项行动态定位: {n_opts} 个按钮, ROI内中心 y={dyn[0][1]} "
                                 f"(硬编码 y={rk.ANTIBOT_OPTION_COORDS[0][1]} 已漂移, 不再使用)")
            else:
                opts = list(rk.ANTIBOT_OPTION_COORDS)
                self.log("warn", "弹窗选项行动态定位失败, 回退硬编码坐标 (若点空请查截图)")
            how = ""
            ox = oy = -1
            if llm_idx and 1 <= llm_idx <= n_opts:
                ox, oy = opts[llm_idx - 1]
                how = f"视觉模型(第{llm_idx}个按钮)"
                if local_hit:
                    lx, ly = local_hit
                    if abs(lx - ox) < 100 and abs(ly - oy) < 100:
                        self.log("info", f"本地算术与模型结论一致 (按钮{llm_idx}), 双重确认")
                    else:
                        self.log("warn", f"本地与模型分歧 (本地按钮@({lx},{ly}) vs 模型按钮{llm_idx}@({ox},{oy}), ROI内坐标), 信模型 (视觉直读)")
            elif local_hit:
                ox, oy = local_hit
                how = "本地算术(模型不可用, 兜底)"
            else:
                self.log("warn", f"模型与本地均未得出答案 (model_idx={llm_idx}), 重试")
                self._save_anomaly(f"antibot_noans{attempt}")
                continue

            # 转回全图绝对坐标后点击
            gx, gy = x0 + int(ox), y0 + int(oy)
            self.log("info", f"✓ 点击按钮 ({how}) @ ({gx},{gy})")
            self._tap(gx, gy)

            # 复查: 弹窗消失=通过; 弹窗仍在=重试
            self._sleep_stop(rk.ANTIBOT_RECHECK_SEC)
            img2 = self._shot_img()
            if img2 is None:
                return "retry"
            if self._check_antibot(img2):
                self.log("warn", "答题后弹窗仍在 (可能选错重出题), 继续重试")
                self._save_anomaly(f"antibot_retry{attempt}")
                continue
            # 弹窗消失. 误判修正: 不要强行要求"荣耀排位赛"标题复现
            #   - 答对: 弹窗消失 -> 直接进入匹配中/战斗 (主页标题被光球特效遮, 不应判踢)
            #   - 答错: 弹窗消失 -> 几秒后跳主城, 主城独有"主城/战盟/好友"等元素
            # 区分: 等 ~6s 走完匹配/战斗过渡, 然后看是不是落到主城 (主城信号 = 真踢)
            self._sleep_stop(6)
            img3 = self._shot_img()
            if img3 is not None and self._is_in_main_city(img3):
                self.log("warn", "答题后被踢回主城 (识别到主城独有元素), 重新导航")
                self._save_anomaly(f"antibot_kicked{attempt}")
                return "kicked"
            # 不在主城: 视为答题通过. 后续由 _wait_battle_ui 接管 (battle/cooldown/antibot)
            self.log("ok", "✓ 防挂机验证已通过 (弹窗消失, 已进入匹配/战斗流程)")
            self._save_stage("antibot_solved")
            return "solved"
        return "giveup"

    _BTN_TPL_MIN = 0.65   # 实测: 有按钮 >=0.73, 无按钮 <=0.60, 0.65 干净分离

    def _find_btn_tpl(self, img: Optional[np.ndarray], fname: str,
                      roi: tuple, min_score: float = 0.0) -> Optional[dict]:
        """固定样式按钮模板匹配 (战斗/结算场景艺术字+光效 OCR 实测经常认不出:
        2026-09-09 16:46 真实战斗截图 '跳过' ROI/全图 OCR 均无命中, 引擎干等超时).
        模板存 assets/<fname>, 取自真实截图; cv2.imread 不支持中文路径故走 imdecode.
        返回 {cx, cy, score}; 未命中/模板缺失 -> None."""
        if img is None:
            return None
        cache_key = "_tpl_" + fname.replace(".", "_")
        tpl = getattr(self, cache_key, None)
        if tpl is None:
            p = Path(__file__).resolve().parent.parent / "assets" / fname
            if not p.exists():
                return None
            buf = np.fromfile(str(p), dtype=np.uint8)
            tpl = cv2.cvtColor(cv2.imdecode(buf, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
            self.__setattr__(cache_key, tpl)
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        x0, y0, x1, y1 = roi
        region = g[y0:y1, x0:x1]
        if region.shape[0] < tpl.shape[0] or region.shape[1] < tpl.shape[1]:
            return None
        r = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(r)
        # NaN 保护: 常量图 (如全黑帧/占位图) 下 TM_CCOEFF_NORMED 可能返回 NaN, 而
        # `nan < 阈值` 恒为 False -> 会被当成"命中", 是隐蔽的误判源. 显式剔除.
        if not np.isfinite(mv) or mv < (min_score or self._BTN_TPL_MIN):
            return None
        return {"cx": x0 + ml[0] + tpl.shape[1] // 2,
                "cy": y0 + ml[1] + tpl.shape[0] // 2,
                "score": float(mv)}

    def _tpl_score(self, img: Optional[np.ndarray], fname: str,
                   roi: tuple) -> Optional[float]:
        """返回模板匹配的**原始最高分** (不做任何阈值判断), 供三值门控使用。

        与 `_find_btn_tpl` 的区别: 后者在分数不达 `min_score` 时返回 None, 拿不到分数;
        门控需要"知道分数落在哪个区间", 故单独提供。模板缺失/区域小于模板/NaN -> None
        (None 表示"无法判断", 调用方应退回 OCR, 而不是当成未命中)。"""
        if img is None:
            return None
        cache_key = "_tpl_" + fname.replace(".", "_")
        tpl = getattr(self, cache_key, None)
        if tpl is None:
            p = Path(__file__).resolve().parent.parent / "assets" / fname
            if not p.exists():
                return None
            buf = np.fromfile(str(p), dtype=np.uint8)
            tpl = cv2.cvtColor(cv2.imdecode(buf, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
            self.__setattr__(cache_key, tpl)
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        x0, y0, x1, y1 = roi
        region = g[y0:y1, x0:x1]
        if region.shape[0] < tpl.shape[0] or region.shape[1] < tpl.shape[1]:
            return None
        r = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
        _, mv, _, _ = cv2.minMaxLoc(r)
        return float(mv) if np.isfinite(mv) else None

    def _hint_or_tpl(self, img: np.ndarray, fname: str, hints: tuple,
                     roi: tuple, hi: float, lo: float) -> bool:
        """固定样式按钮的**三值模板门控** (P2, 2026-09-13): 快 2 个数量级地判掉
        "确定在 / 确定不在" 两种常见情形, 只把**不确定**的帧交回 OCR。

        score >= hi -> True  (命中, 跳过 OCR, ~0.07s)
        score <= lo -> False (未命中, 跳过 OCR, ~0.07s)
        中间       -> OCR 关键词兜底 (3~8s, 与旧行为一致)

        为什么两个"跳过 OCR"分支都安全: 见 rk.END_TPL_HI 常量处的实测依据 ——
        `hi` 高于全部负样本、`lo` 低于全部正样本 (含 33 种退化), 因此常见的两个方向
        都能被模板直接定论; 落进中间区间的帧一律走 OCR, **只慢不错**。
        旧写法 `_find_btn_tpl(...) or _find_hint(...)` 在模板未命中时**必然**跑一次
        OCR —— 而主页帧恰好永远是这个分支, 这正是落点判定 44s 中的主要浪费。

        ⚠️ 已知取舍 (2026-09-13 明确记录, 不要当成 bug): 当模板给出**低分**时, 本方法
        **不再跑 OCR**。因此若某天游戏改版换了按钮美术, 会出现"文本仍可读但模板不匹配"
        ⇒ 判不出结算页。实测该组合在当前资产下不可达 (真实 end 页 0.999/1.000、
        back 页 1.000, 均 >= HI), 且失效时 `_wait_btn` 会打"未命中模板, 改用 OCR 兜底"
        的告警作为漂移信号。复核方式: 重跑 scripts/verify_tpl_gate.py。"""
        sc = self._tpl_score(img, fname, roi)
        if sc is not None:
            if sc >= hi:
                return True
            if sc <= lo:
                return False
        return bool(self._find_hint(img, hints, roi=roi, scale=1.0))

    def _wait_btn(self, fname: str, tpl_roi: tuple, ocr_hints: tuple,
                  ocr_roi: tuple, timeout: float, desc: str) -> Optional[dict]:
        """等一个固定样式按钮: 模板匹配优先 (~20-140ms), OCR 文字兜底 (防游戏改样式).
        返回命中 {cx, cy, score, how}; 超时/停止 -> None (调用方用 stop_evt 区分)。

        2026-09-13 P2 漂移探针: 走到 OCR 兜底说明"固定样式按钮的模板没命中", 这是
        **模板资产失效 (游戏改版换美术) 的唯一运行时信号** —— P2 门控在高分/低分两档
        都跳过 OCR, 静默性变强了, 所以这里的告警必须保留且显眼。见到它就该重跑
        `scripts/verify_tpl_gate.py` 并更新 assets/ 下的模板。"""
        def f():
            img = self._shot_img()
            if img is None:
                return None
            hit = self._find_btn_tpl(img, fname, tpl_roi)
            if hit:
                hit["how"] = "模板"
                return hit
            o = self._find_hint(img, ocr_hints, roi=ocr_roi, scale=1.0)
            if o:
                o = dict(o)
                o["how"] = "OCR"
                self.log("warn", f"⚠ 按钮 {fname} 未命中模板, 改用 OCR 兜底 "
                                 f"(模板可能已失效, 建议复核 assets/ 与该模板门控阈值)")
            return o

        def tick():
            self._progress("settle", desc)

        return self._wait_until(f, timeout, 1.0, desc, tick=tick)

    def _find_skip_btn(self, img: Optional[np.ndarray] = None) -> Optional[dict]:
        """跳过按钮: 模板匹配 (兼容旧调用点)."""
        return self._find_btn_tpl(img, "skip_btn.png", rk.SKIP_ROI)

    # X2 加速按钮模板阈值. 实测 (2026-09-12, 10 张正负样本):
    #   战斗页 0.973 / 1.000 / 1.000 / 1.000;  非战斗页 ≤ 0.601 -> 0.75 干净分离.
    _X2_TPL_MIN = 0.75

    def _is_in_battle(self, img: Optional[np.ndarray] = None,
                      fast: bool = False) -> bool:
        """统一战斗页判据 —— 战斗页 HUD 双锚点: 右下「跳过」+ 其左侧「X2」加速按钮.

        P0 (2026-09-12). 本方法用于消灭"同一语义多处各自实现": _classify_page 早就能
        认出 battle, 但 _enter_pvp_hub 另写了一套 (只认 主页/二级菜单/主城), 能力不一致
        -> 战斗中重导航被判"无法识别" -> unknown_screen 终止任务.
        今后凡判断"是否在战斗页", 一律走本方法.

        **为什么必须双锚点** (2026-09-12 实测, 这点是后续踩坑的关键):
        单用 skip_btn.png 模板既不充分也不特异 ——
          · 漏判: 战斗页光效干扰下分数跌到 0.632 / 0.650 (阈值 0.65, 差 0.0003 就漏);
          · 误判: 擂台页右下「创建擂台」按钮形状相似, 分数 0.718 > 0.65 阈值 ->
                  会被当成战斗页, 进而对非战斗页乱点「跳过」坐标.
        加入 x2_btn.png 后两者同时出现才算战斗页, 正负样本 10/10 分离.

        判定顺序 (先便宜后鲁棒):
          1) 双模板各 ~20ms: 「跳过」命中 且 「X2」命中 -> 战斗页;
          2) OCR SKIP_ROI 出现「跳过」或「X2」(~0.5-1.5s) —— 光效帧下的唯一退路
             (实测 OCR 本身也不稳定: 同一战斗页有时只读到 "X2", 有时读到 "跳过",
              故两个词任一命中即可).
        fast=True: 只做第 1 步 (~20ms), 用于"已知处于战斗流程"的热路径快速确认.
        """
        if img is None:
            img = self._shot_img()
        if img is None:
            return False
        if (self._find_skip_btn(img) is not None
                and self._find_btn_tpl(img, "x2_btn.png", rk.X2_ROI,
                                       min_score=self._X2_TPL_MIN) is not None):
            return True
        if fast:
            return False
        # 大小写两种写法都给, OCR 对 "X2" 的识别大小写不稳定
        return self._find_hint(img, ("跳过", "X2", "x2"),
                               roi=rk.SKIP_ROI, scale=1.0) is not None

    def _wait_battle_ui(self, glory: bool = False) -> str:
        """等比赛界面: 全图 OCR 找 '跳过' (右下出现). 等待期每 1s 顺带检测冷却/每日上限弹窗.
        glory=True (荣耀时刻): 跳过冷却/每日上限弹窗检测 (游戏规则荣耀窗口两者都不会弹,
        纯 OCR 开销会拖慢快节奏的荣耀轮询), 防挂机检测放宽到 8s 后才启用 (40 局 0 触发).
        返回: 'battle' = 出现跳过/可开赛; 'cooldown' = 检测到冷却弹窗 (任务应停);
              'daily_limit' = 检测到每日 30 次上限弹窗 (当日不可解除, 任务应终止);
              'antibot' = 检测到防挂机验证弹窗 (等待主循环处理);
              'main_city' = 被弹回主城 (连续 2 次识别到主城独有元素, 主循环自愈重导航);
              'pve_stage' = 落在 PvE 副本关卡页 (引擎绝不该在, 主循环按返回键逃生);
              'game_gone' = 游戏进程被回收/被切到桌面 (2026-09-13, 主循环拉起后重导航);
              'session' = 会话失效弹窗 (token失效) / 停在登录页 (2026-09-13, 主循环重新登录后重导航);
              'stopped' = 收到停止请求 (等待被打断);
              'timeout' = 等待超时."""
        t0 = time.time()

        def f():
            img = self._shot_img()
            if img is None:
                return None
            if not glory:
                if self._check_daily_limit(img):
                    return "daily_limit"
                if self._check_rank_cooldown(img):
                    return "cooldown"
            # 统一战斗页判据 (模板匹配 ~20ms, 未命中再 OCR 右下 ROI 兜底:
            # 战斗场景艺术字 OCR 常认不出, 但"跳过"二字 OCR 稳定可读; 两者互补)
            if self._is_in_battle(img):
                return "battle"
            if time.time() - t0 > (8.0 if glory else 3.0) and self._check_antibot(img):
                # 点完匹配后若长时间既不进比赛也无正常排队, 且出现验证弹窗
                # (荣耀快节奏: 3s 内跳过常已出现, 防挂机 40 局 0 触发, 放宽到 8s 省每轮一次 ROI OCR)
                return "antibot"
            # 快速失败: 匹配等待 >15s 后若连续 2 次识别到主城 (被弹回), 立即上报
            # 而不是干等 200s 超时 (2026-09-09 16:19 实例: 弹回主城干等 331s 报 no_battle_ui)
            if time.time() - t0 > 15:
                if self._is_in_main_city(img):
                    f.main_hits = getattr(f, "main_hits", 0) + 1
                    if f.main_hits >= 2:
                        return "main_city"
                else:
                    f.main_hits = 0
            # 快速失败 (2026-09-12): 匹配等待 >20s 后若发现落在 PvE 副本关卡页 (引擎绝不该在),
            # 立即上报交主循环逃生, 不必干等满 200s 才报 no_battle_ui.
            # 实测 13:41:08 点单人匹配 -> 13:46:44 才超时报"页面无法识别"(实为副本章节图).
            # 只查一次 (副本页是静态页), 避免每秒一次全图 OCR 拖慢热路径.
            # 会话失效 / 登录页一次性探查 (2026-09-13): 8s 后查一次 (小 ROI OCR ~1s)。
            # 不查的话, 弹窗期间所有判据都不命中 -> 干等满 200s 才超时, 才由 _classify_page 发现。
            if time.time() - t0 > 8 and not getattr(f, "session_checked", False):
                f.session_checked = True
                if self._is_session_expired(img) or self._is_login_screen(img):
                    return "session"
            if time.time() - t0 > 20 and not getattr(f, "pve_checked", False):
                f.pve_checked = True
                if self._is_pve_stage(img):
                    return "pve_stage"
            # 游戏失联快速失败 (2026-09-13 新增): 匹配等待期间游戏进程若被模拟器回收 /
            # 被切到桌面, 所有页面判据只会返回 unknown -> 干等满 200s 才报 no_battle_ui,
            # 而返回键逃生对桌面无效。实测 09-13 11:38~11:43 一次白烧 4 分钟且 0 场。
            # 25s 后一次性探查 (dumpsys+pidof ~1.5s, 每轮最多一次, 不拖慢正常热路径)。
            if time.time() - t0 > 25 and not getattr(f, "health_checked", False):
                f.health_checked = True
                if self._check_game_health() in ("gone", "background"):
                    return "game_gone"
            return None

        def tick():
            self._progress("battle", f"匹配/倒计时中 {int(time.time() - t0)}s")

        ret = self._wait_until(f, rk.WAIT_BATTLE_UI_SEC, 1.0,
                               "等待比赛界面(右下跳过)", tick=tick)
        if ret in ("battle", "cooldown", "antibot", "main_city", "daily_limit",
                   "pve_stage", "game_gone", "session"):
            return ret
        return "stopped" if self.stop_evt.is_set() else "timeout"

    def _tap_skip_quick(self):
        """比赛界面直接点跳过 (排位不等 10s)。检测顺序:
        1) 模板匹配 SKIP_ROI (~20ms, 战斗场景 OCR 认不出);
        2) OCR 右下 ROI 找中心 (兜底);
        3) 固定坐标 SKIP_FALLBACK (最后兜底)."""
        img = self.last_img if self.last_img is not None else self._shot_img()
        if img is None:
            return False
        hit = self._find_skip_btn(img)
        if hit:
            self._tap(hit["cx"], hit["cy"])
        else:
            hit2 = self._find_hint(img, rk.SKIP_HINTS, roi=rk.SKIP_ROI, scale=1.0)
            if hit2:
                self._tap(hit2["cx"], hit2["cy"])
            else:
                fx, fy = rk.SKIP_FALLBACK
                self._tap(fx, fy)
        self.log("info", "✓ 点击 跳过 (比赛立即结束)")
        return True

    def _tap_third_box(self):
        """奖励页直接点第三个宝箱固定坐标 (OCR 在宝箱图形上读不到文字, 用实测坐标).
        实测宝箱 y 中心 600; 三宝箱 x 从左到右 370 / 770 / 1180."""
        fx, fy = rk.THIRD_BOX_FALLBACK
        self._tap(fx, fy)
        self.log("info", f"✓ 点击 第三个宝箱 @ ({fx},{fy})")
        return True

    def _judge_rank_result(self, img: np.ndarray) -> Optional[str]:
        """在结算/战绩页判定本局胜负: 结算页标题'挑战成功/挑战失败'(y≈98) 或 战绩页大字
        WIN(227,153)/LOSE(925,153). 两者都在屏幕顶部带 -> 只 OCR VERDICT_ROI (全图 3.5s -> ~0.2s).
        返回 'win' / 'loss' / None(两处关键词都没读到)。"""
        win_kw = ("挑战成功", "胜利", "成功", "WIN")
        loss_kw = ("挑战失败", "战败", "惜败", "失败", "LOSE")
        items = self._ocr_texts(img, roi=rk.VERDICT_ROI, scale=1.0)
        for it in items:
            t = it["text"].upper()
            if any(k.upper() in t for k in win_kw):
                return "win"
        for it in items:
            t = it["text"].upper()
            if any(k.upper() in t for k in loss_kw):
                return "loss"
        return None

    def _apply_rank_verdict(self, verdict: Optional[str], where: str) -> None:
        """把判出的本局胜负计入统计并打日志 (rank 模式无对手档案读写)."""
        if verdict == "win":
            self.wins += 1
            self.log("ok", f"✓ 本局胜利 ({where})")
            self._progress("settle", "本局胜利 ✓")
        elif verdict == "loss":
            self.losses += 1
            self.log("warn", f"✗ 本局战败 ({where}) → 记一败")
            self._progress("settle", "本局战败 ✗")
        else:
            self.log("warn", f"({where}) 未识别出胜负文字, 本局战绩记为未知")

    def _try_fast_hub(self, img: np.ndarray, is_glory: bool) -> bool:
        """稳态快车道判定 (2026-09-13 提速): 本局起点是否可跳过"结算/战斗/上限"三项检查.

        触发条件: 上一局刚走完"结算链 -> 返回大厅" (`_at_hub_confident` = True),
        此时起点**必然是排位主页**, 只需屏幕上的主页独有匹配按钮 OCR 命中即可确认.

        为什么能省: 被跳过的三项全是"来源不确定"入口的防御 ——
          · `_detect_settle_stage` (3 次 ROI OCR) 防"重导航落在上一局残留的宝箱页";
          · `_is_in_battle` (1 次 ROI OCR) 防"导航落点直接是战斗页";
          · `_check_daily_limit` (1 次 ROI OCR) 防"30 次上限弹窗盖在主页上".
        稳态起点不存在这三种情况, 而它们实测合计 ~15s OCR —— 正是"点单人匹配迟到
        46~60s、倒计时归零被踢回主城"的主要残余.

        安全: ① 只用"单人匹配/取消匹配"(主页独有且必居其一)验证, 比 HUB_UNIQUE_HINTS
              全集更特异 (不会把 组队匹配 当通过); ② 验证不过 -> 返回 False,
              调用方照旧跑完整判定链, **不会盲点**; ③ 标志一次性消费, 防同局反复试.

        返回 True = 本局走快车道 (调用方置 fast_hub).
        """
        if not self._at_hub_confident:
            return False
        self._at_hub_confident = False
        if is_glory:
            return False    # 荣耀时刻靠"自动参赛", 不点匹配, 此标志无意义 -> 消费掉
        hit = self._find_hint(img, ("单人匹配", "取消匹配"),
                              roi=rk.HUB_BTN_ROI, scale=1.0)
        if hit is None:
            return False
        self.log("info", f"稳态快车道: 主页已确认 (匹配按钮「{hit['match']}」在屏), "
                         f"本局省去结算/战斗/30次上限三项检查")
        return True

    def _run_rank(self, started: _dt.datetime, glory: bool = False) -> dict:
        """排位赛固定序列:
        日常 (glory=False): 每轮 单人匹配 -> 冷却检测 -> 等比赛界面 -> 跳过 -> 宝箱 -> 结束 -> 返回大厅.
        荣耀时刻 (glory=True): 18-20 系统已勾"自动参与"自动开赛, **无需点单人匹配**,
          每轮直接 等跳过出现 -> 跳过 -> 宝箱 -> 结束 -> 返回大厅.
        不读不写对手档案; 日常按 self.rounds 场数停, 荣耀时刻 rounds=0 表示打到 20:00 或手动停止."""
        # 荣耀时刻: 强制等待比赛超时放宽到 3.5min (2min 准备 + 观赛缓冲), 且不设场数上限
        is_glory = glory
        if is_glory:
            from sj_bot.state_machine import in_glory_window
            target = self.rounds or 0  # 0 = 一直打到手动停止
            if not in_glory_window():
                self.log("warn", "当前不在荣耀时刻窗口 (每晚 18:00-20:00), 任务直接结束")
                return self._summary("not_glory_window", started)
            self.log("info", "荣耀时刻挂机开始 (18-20 自动参赛): 循环 等跳过->跳过->宝箱->结束->返回大厅"
                             f"{', 目标 ' + str(target) + ' 局' if target > 0 else ', 打到停止'}")
        else:
            target = self.rounds or 0
            if target <= 0:
                self.log("error", "排位赛需要指定 rounds>=1")
                return self._summary("bad_rounds", started)
            self.log("info", f"排位赛自动打开始: 目标 {target} 局 (固定序列 OCR 状态机)")
        # 日常: 起步必须先到主页 (否则会乱点). 荣耀时刻: 条件导航 (2026-09-10 用户实测:
        # 主城直接启动荣耀 -> 旧逻辑完全跳过导航, 在主城傻等跳过按钮).
        # 先看当前页: 战斗中(跳过可见)或已在荣耀排位主页 -> 直接开始; 否则导航进荣耀排位赛.
        # (已在比赛中时导航反而会打断当前局, 所以要条件判断而非一律导航)
        if is_glory:
            img0 = self._shot_img()
            in_battle = img0 is not None and self._is_in_battle(img0)
            on_hub = img0 is not None and self._is_rank_hub(img0)
            if in_battle or on_hub:
                self.log("info", f"荣耀起步: 已在{'战斗中(跳过可见)' if in_battle else '荣耀排位主页'}, 直接进入自动参赛循环")
            else:
                self.log("info", "荣耀起步: 不在荣耀排位主页/战斗中, 先导航至荣耀排位赛")
                if not self._enter_pvp_hub_retry("rank"):
                    return self._summary("nav_failed", started)
        else:
            if not self._enter_pvp_hub_retry("rank"):
                return self._summary("nav_failed", started)

        exit_status = "finished"  # 循环中途 break 时覆盖: 冷却/异常
        while (is_glory or self.fought < target) and not self.stop_evt.is_set():
            if is_glory:
                # 荣耀时刻收尾: 已过 20:00 窗口 -> 任务自然结束
                from sj_bot.state_machine import in_glory_window
                if not in_glory_window():
                    self.log("info", "荣耀时刻窗口(18-20)已结束, 任务自动收尾")
                    exit_status = "glory_done"
                    break
                self._progress("wait", f"荣耀时刻等待自动开赛 (已打 {self.fought} 局)")
            else:
                # 跨窗口无缝切换 (2026-09-09 用户需求): 日常排位跨过 18:00 ->
                # 下一局直接转荣耀自动参赛 (免点匹配), 打到 20:00 glory_done 收尾.
                # 场景: 17:5x 启动排位, 最后一局打完已过 18:00, 引擎自动接管荣耀.
                from sj_bot.state_machine import in_glory_window
                if in_glory_window():
                    is_glory = True
                    self.log("warn", "已进入荣耀时刻窗口 (18:00-20:00), 日常排位无缝切换为"
                                     "自动参赛模式 (剩余目标场数作废, 打到 20:00 自动收尾)")
                    continue
                self.log("info", f"开始第 {self.fought + 1}/{target} 局")
            # 2026-09-13 可观测性: 记本局起点时间。用户最关心的指标是
            # "起步 -> 点单人匹配" 的秒数 (超过倒计时就会归零被踢回主城),
            # 在 _step_single_match 里把该差值写进日志, 免得靠人工比对时间戳。
            self._round_t0 = time.time()

            # ---- 0. 主页断言 + 日常点单人匹配: 合并为单次截图 (省 ~1.5s/局) ----
            # 非 None = 本局起点已在结算链某页 (宝箱/比赛结束/返回大厅), 直接接管结算.
            # 用实例属性而非局部变量: _wait_battle_ui 的超时自愈也可能识别出结算页,
            # 需要把结论传回主循环 (方法作用域内改不到局部变量).
            self._pre_settle_stage: Optional[str] = None
            # ---- 游戏存活预检 (2026-09-13): 便宜 (pidof ~0.3s) 但能拦住最贵的一类失败 ——
            # 游戏进程若已被模拟器回收, 本局后面每一步判据都只会返回 unknown, 白烧一整局
            # (匹配等待 200s + 逃生连按 4 次返回键) 才终止。这里先花 ~0.3s 拦住它。
            # 只在 pidof 为空时才升级为完整前台校验 (~1.5s), 正常情况几乎零开销。
            if self._game_pid() == "":
                st = self._check_game_health()
                if st in ("gone", "background"):
                    self.log("warn", f"本局开始前检测到游戏不在前台 (状态={st}), 先拉起游戏")
                    if not self._ensure_game_foreground("round_start"):
                        self.log("error", "游戏无法拉起, 任务终止")
                        exit_status = "game_gone"
                        break
                    if not self._enter_pvp_hub_retry("rank"):
                        exit_status = "game_gone_nav"
                        break
                    continue
            img = self._shot_img()
            if img is None:
                self.errors += 1
                self._sleep_stop(5)
                continue
            # ---- 稳态快车道 (2026-09-13 提速, 解决"点击单人匹配迟到"的余量) ----
            # 上一局刚走完"结算链 -> 返回大厅", 起点必然在排位主页 -> 只要 1 次小 ROI OCR
            # 确认主页独有的匹配按钮即可, 跳过下面三个昂贵检查:
            #   _detect_settle_stage (3 次 ROI OCR ~9s) + _is_in_battle (~2s)
            #   + _check_daily_limit (~4s). 实测这几项是点击晚 46~60s 的主要残余.
            # 为什么平时不能省: 冷却续打 / 被踢回主城后重导航 / 防挂机后重导航这些
            # **来源不确定**的入口, 可能正好落在上一局残留的宝箱页上 (09-12 实测),
            # 而宝箱页背景就是排位主页长相 —— 少一次结算判定就会误点"单人匹配"白等 200s.
            # 快车道只在来源确定时开, 且必须通过"主页独有按钮"验证; 验证不过就退回完整链.
            fast_hub = self._try_fast_hub(img, is_glory)

            # ★ 判定顺序: **结算链优先于战斗页** (2026-09-12).
            #   结算层 (宝箱/比赛结束/返回大厅) 是叠加在战斗画面之上的半透明层,
            #   右下角"跳过/X2"、左下角"自动战斗"都还在 —— 实测 rank_3_reward.png 上
            #   x2 模板 1.000 / 跳过模板 0.868, 与真战斗页几乎不可分.
            #   若先判战斗页, 就会对着宝箱页去点"跳过"坐标 (乱点); 而语义上此刻该做的是
            #   "选宝箱 -> 比赛结束 -> 返回大厅". 顺序与 _classify_page 保持一致.
            self._pre_settle_stage = None if fast_hub else self._detect_settle_stage(img)
            if self._pre_settle_stage:
                self.log("info", f"当前停在结算链 ({self._pre_settle_stage}), "
                                 f"直接接管结算流程 (结算层覆盖在战斗画面上, 不能当战斗页)")
                in_battle0 = False
            elif fast_hub:
                # 稳态快车道已用屏幕上的匹配按钮确认在主页 -> 无需再跑战斗页/主页断言.
                in_battle0 = False
            # P0 (2026-09-11 根因修复) 战斗页接管: 若当前已在战斗中 (跳过按钮可见 —
            # 导航落点=battle, 或上一局残留), 不再要求"必须在主页", 直接走 跳过 -> 结算,
            # 不退回主页重新匹配. 否则会被下方 not_rank_hub 断言当场干掉 (与导航误判同源).
            elif self._is_in_battle(img):
                in_battle0 = True
                self.log("info", "当前已在战斗页 (跳过可见), 直接接管本局, 不重新匹配")
            # 日常: 必须主页 (否则乱点); 荣耀时刻: 跳过主页断言, 容忍任意状态
            # (开赛后下一局也可能因自动参与即刻进入匹配, 主页断言会导致连续 not_rank_hub)
            elif not is_glory:
                in_battle0 = False
                if not self._is_rank_hub(img, fast=True) and not self._is_rank_hub(img):
                    if self.stop_evt.is_set():
                        break
                    # P0 (2026-09-12 根因修复): 原文在此**一击终止**任务. 实测该分支
                    # 大量命中"页面过渡态"而非真异常 —— 典型: 导航刚落点/战斗页正在加载
                    # (跳过按钮尚未渲染), 或匹配失败被系统踢回主城. 09-12 11:13 实例:
                    # 11:13:09 导航判定 battle 成功 -> 循环头 14s 后重判却两者皆非 ->
                    # not_rank_hub 收工 (设 23 场只打 3 场); 而 _save_anomaly 补拍的图
                    # 恰恰是标准战斗页.
                    # 改为三层自愈, 不再因单次判定失败就杀任务:
                    #   ① 过渡缓冲: 等 5s 让页面加载完, 复查一次;
                    #   ② 仍不认 -> 重新导航回排位赛主页 (见下面 _enter_pvp_hub_retry("rank"));
                    #      2026-09-22 起改由 _selfheal_gate 统管次数: **窗口内不限次数**
                    #      (用户口径: "到指定排位时间了就要进入排位界面"), 只有窗口关闭
                    #      或窗口内连续 SELFHEAL_STALL_SEC 无进展才收尾; 日常 rank 无窗口
                    #      概念, 仍是 SELFHEAL_LEGACY_MAX 次上限 (旧语义保持不变);
                    #   ③ 闸门判停才算真异常收尾, 精确原因写入 _last_stop_kind (供页面展示).
                    self._sleep_stop(5)
                    img2 = self._shot_img()
                    if img2 is not None and (self._is_in_battle(img2)
                                             or self._is_rank_hub(img2)):
                        self.log("info", "页面过渡完成 (已就绪), 重新判定本局起点")
                        continue
                    if not self._selfheal_ok("nav_rescues", "既不在排位主页也不在战斗页"):
                        self._save_anomaly("not_rank_hub")
                        exit_status = self._stop_reason("not_rank_hub")
                        break
                    self._selfheal_pause(self.nav_rescues)
                    self.log("warn", f"既不在排位主页也不在战斗页 (可能被踢回主城/页面异常), "
                                     f"重新导航后重试本局 (第 {self.nav_rescues} 次)")
                    self._save_anomaly(f"not_rank_hub_rescue{self.nav_rescues}")
                    if not self._enter_pvp_hub_retry("rank"):
                        exit_status = "nav_fail"
                        break
                    continue
            else:
                in_battle0 = False
                # 荣耀时刻: 若在主页, 等待开赛; 若在战斗中, 跳过本断言 (后续 _wait_battle_ui 接管)
                if self._is_rank_hub(img, fast=True):
                    self._progress("wait", "荣耀时刻在主页, 等待系统自动开赛")

            # ---- 1. 日常: 点单人匹配 (荣耀时刻跳过, 系统自动参赛) ----
            if self._pre_settle_stage:
                self.log("info", f"已在结算链 ({self._pre_settle_stage}), 跳过点击匹配")
            elif in_battle0:
                self.log("info", "已在战斗页, 跳过点击匹配, 直接进入跳过流程")
            elif not is_glory:
                # 30 次上限预检: 复用第 0 步截图, 已达上限就不再浪费点击 (18:08 截图表明
                # 上限提示会直接显示在主页, 不点匹配也能看到).
                # 稳态快车道跳过此预检 (~4s OCR): 上限弹窗会挡住主页匹配按钮 ->
                # 快车道的按钮验证必然失败, 于是退回完整链由它检测; 即失败也只会
                # 落到 _wait_battle_ui, 它 1~3s 内就能识别出该弹窗. 不会盲点.
                if not fast_hub and self._check_daily_limit(img):
                    self.log("error", "今日累计挑战已达 30 次上限, 游戏提示请明日再来"
                                      " (18-20 点荣耀时刻不受限), 任务结束")
                    exit_status = "daily_limit"
                    break
                self._progress("scan", "点击单人匹配" + (" (稳态快车道)" if fast_hub else ""))
                if not self._step_single_match(img):   # 复用第 0 步截图
                    if self.stop_evt.is_set():
                        break
                    self._save_anomaly("single_match_fail")
                    exit_status = "single_match_fail"
                    break
            else:
                self.log("info", "荣耀时刻: 跳过单人匹配, 等待系统自动开赛 (跳过按钮出现)")

            # ---- 2. 等比赛界面 (等待期顺带检测冷却/防挂机弹窗) ----
            if self._pre_settle_stage:
                # 本局起点已在结算链: 无需等待比赛界面, 直接落到结算流程 (下方 stage 初值接管)
                ret = "battle"
            else:
                self._progress("battle", "等待比赛界面")
                if in_battle0:
                    ret = "battle"   # 已在战斗页: 直接进跳过流程 (跳过匹配等待与超时自愈)
                else:
                    ret = self._wait_battle_ui(glory=is_glory)
            if ret == "stopped":
                break  # 手动停止: 直接收尾 (summary 统一归为 stopped, 不报假超时)
            if ret == "game_gone":
                # 游戏失联 (进程被回收/被切桌面): 拉起 + 重导航后续跑, 不消耗场数.
                rr = self._recover_from_game_loss()
                if rr == "recovered":
                    continue
                exit_status = self._stop_reason("game_gone") if rr == "stop" else "game_gone_nav"
                break
            if ret == "session":
                # 会话失效 / 停在登录页: 关弹窗 -> 重新登录 -> 重导航, 不消耗场数.
                # (2026-09-13 实测: 该弹窗半透明盖在战斗画面上, 不特判就会反复点跳过直到超限终止)
                rr = self._recover_from_session("match_wait")
                if rr == "recovered":
                    continue
                exit_status = self._stop_reason("session") if rr == "stop" else "session_nav"
                break
            if ret == "daily_limit":
                self.log("error", "今日累计挑战已达 30 次上限, 游戏提示请明日再来"
                                  " (18-20 点荣耀时刻不受限), 任务结束")
                exit_status = "daily_limit"
                break
            if ret == "cooldown":
                if self.cooldown_waits >= rk.MAX_COOLDOWN_WAITS:
                    self.log("error", f"冷却等待已达上限 {rk.MAX_COOLDOWN_WAITS} 次, 任务终止")
                    exit_status = "cooldown"
                    break
                w = self._wait_out_cooldown()
                if w == "stopped":
                    break
                if w == "nav_fail":
                    exit_status = "cooldown_nav_fail"
                    break
                continue   # 冷却结束已重导航, 重试本局 (fought 未消耗)
            if ret == "antibot":
                # 防挂机验证弹窗: 尝试答题; solved 后重新等开赛, 其它结果终止本轮
                solved = self._solve_antibot()
                if solved == "kicked":
                    # 选错被踢回主城: 重新导航回排位主页, **回循环头重新点匹配**.
                    # 历史 BUG (2026-09-10 实锤): 旧代码重导航后直接 _wait_battle_ui 干等,
                    # 但没人重新点单人匹配 -> 匹配从未开始 -> 200s 必超时 no_battle_ui_after_antibot.
                    self.log("warn", "防挂机答错被踢回主城, 重新导航回排位主页后重试本局")
                    self._sleep_stop(2)
                    if not self._enter_pvp_hub_retry("rank"):
                        exit_status = "antibot_kicked_nav"
                        break
                    continue
                elif solved != "solved":
                    self.log("warn", f"防挂机验证未能通过 ({solved}), 本轮任务停止")
                    exit_status = f"antibot_{solved}"
                    break
                else:
                    self._progress("battle", "防挂机通过, 继续等待比赛界面")
                    ret = self._wait_battle_ui(glory=is_glory)
                if ret == "game_gone":
                    rr = self._recover_from_game_loss()
                    if rr == "recovered":
                        continue
                    exit_status = self._stop_reason("game_gone") if rr == "stop" else "game_gone_nav"
                    break
                if ret == "session":
                    rr = self._recover_from_session("antibot_wait")
                    if rr == "recovered":
                        continue
                    exit_status = self._stop_reason("session") if rr == "stop" else "session_nav"
                    break
                if ret == "cooldown":
                    if self.cooldown_waits >= rk.MAX_COOLDOWN_WAITS:
                        self.log("error", f"冷却等待已达上限 {rk.MAX_COOLDOWN_WAITS} 次, 任务终止")
                        exit_status = "cooldown"
                        break
                    w = self._wait_out_cooldown()
                    if w == "stopped":
                        break
                    if w == "nav_fail":
                        exit_status = "cooldown_nav_fail"
                        break
                    continue
                if ret == "stopped":
                    break  # 手动停止: 直接收尾
                if ret == "daily_limit":
                    self.log("error", "今日累计挑战已达 30 次上限, 游戏提示请明日再来"
                                      " (18-20 点荣耀时刻不受限), 任务结束")
                    exit_status = "daily_limit"
                    break
                if ret == "main_city":
                    # 防挂机通过后仍被弹回主城 (2026-09-14 11:04 实锤): 防挂机弹窗悬停期间
                    # 匹配倒计时归零, 即使答对, 游戏也取消匹配踢回主城 —— 正是用户描述的
                    # "倒计时归零 -> 卡住 -> 踢回主城" 场景。旧代码内层不认 main_city,
                    # 落到下方兜底直接终止 (冤枉: 同一状态在外层第一段等待有自愈)。
                    # 与外层同款自愈: 共享 main_city_rescues 预算 (>3 终止), 重导航后重试本局。
                    if not self._selfheal_ok("main_city_rescues", "防挂机后被弹回主城"):
                        self._save_anomaly("main_city_loop")
                        exit_status = self._stop_reason("main_city_loop")
                        break
                    self._selfheal_pause(self.main_city_rescues)
                    self.log("warn", f"防挂机通过但匹配已失效 (倒计时归零被弹回主城), "
                                     f"重新导航后重试本局 (第 {self.main_city_rescues} 次)")
                    self._save_anomaly(f"main_city_rescue{self.main_city_rescues}")
                    self._sleep_stop(2)
                    if not self._enter_pvp_hub_retry("rank"):
                        exit_status = "main_city_nav_fail"
                        break
                    continue  # 重试本局 (fought 未增, 不消耗场数)
                if ret == "pve_stage":
                    # 防挂机等待期间落在 PvE 副本页 (引擎绝不该在): 返回键逃生回可识别页,
                    # 退回主城则重导航续跑 (与外层超时自愈同款, 2026-09-12 语义)。
                    self.log("warn", "防挂机等待期间落在 PvE 副本关卡页, 按返回键逃生后重试本局")
                    self._save_anomaly("pve_stage_detected")
                    landed = self._escape_unknown_page("pve_stage")
                    if landed is None:
                        exit_status = self._stop_reason("no_battle_ui")
                        break
                    if landed == "main_city":
                        self._sleep_stop(2)
                        if not self._enter_pvp_hub_retry("rank"):
                            exit_status = "main_city_nav_fail"
                            break
                    continue
                if ret != "battle":
                    self.log("error", f"防挂机处理后仍未等到比赛界面 (未见跳过, ret={ret})")
                    self._save_anomaly("no_battle_ui_after_antibot")
                    exit_status = "no_battle_ui"
                    break
            if ret == "stopped":
                break  # 手动停止: 直接收尾 (summary 统一归为 stopped)
            if ret == "main_city":
                # 匹配等待中被弹回主城: 自愈重导航后重试本局 (窗口内不限次数, 见 _selfheal_gate)
                if not self._selfheal_ok("main_city_rescues", "匹配等待中被弹回主城"):
                    self._save_anomaly("main_city_loop")
                    exit_status = self._stop_reason("main_city_loop")
                    break
                self._selfheal_pause(self.main_city_rescues)
                self.log("warn", f"匹配等待中发现被弹回主城, 重新导航后重试本局 "
                                 f"(第 {self.main_city_rescues} 次)")
                self._save_anomaly(f"main_city_rescue{self.main_city_rescues}")
                self._sleep_stop(2)
                if not self._enter_pvp_hub_retry("rank"):
                    exit_status = "main_city_nav_fail"
                    break
                continue  # 重试本局 (fought 未增, 不消耗场数)
            if ret != "battle":
                # 荣耀时刻: 若等跳过超时同时窗口已关 -> 升级为 glory_done (避免误报 no_battle_ui)
                # 根因: 19:59:50 收尾→循环头 in_glory_window=True → _wait_battle_ui 200s 等不到 20:00 后下一场
                if is_glory:
                    from sj_bot.state_machine import in_glory_window
                    if not in_glory_window():
                        self.log("info", "荣耀时刻窗口(18-20)已结束, 等跳过超时 -> 升级为 glory_done")
                        exit_status = "glory_done"
                        break
                # ---- 超时自愈 (2026-09-10): 先识别页面, 认识的按语义处理, 别急着放弃 ----
                # 今早案例: 防挂机处理后干等 200s 收工, 实际页面是有语义的 (主城/主页/弹窗)
                if not self._selfheal_ok("match_rescues", "等待比赛界面超时"):
                    self._save_anomaly("match_rescue_loop")
                    exit_status = self._stop_reason("no_battle_ui")
                    break
                self._selfheal_pause(self.match_rescues)
                page = self._classify_page()
                self.log("warn", f"等待比赛界面超时, 页面识别: {page} (自愈 第 {self.match_rescues} 次)")
                if page == "battle":
                    self.log("info", "识别到跳过按钮已出现, 继续本局")
                    ret = "battle"          # 控制流落到下方 _tap_skip_quick
                elif page == "hub":
                    self.log("warn", "识别到仍在排位主页 (匹配点击疑被弹窗吞掉), 重点匹配")
                    continue
                elif page == "main_city":
                    self.log("warn", "识别到被弹回主城, 重新导航后重试本局")
                    self._sleep_stop(2)
                    if not self._enter_pvp_hub_retry("rank"):
                        exit_status = "main_city_nav_fail"
                        break
                    continue
                elif page == "daily_limit":
                    self.log("error", "识别到每日 30 次上限弹窗, 任务结束")
                    exit_status = "daily_limit"
                    break
                elif page == "cooldown":
                    if self.cooldown_waits >= rk.MAX_COOLDOWN_WAITS:
                        exit_status = "cooldown"
                        break
                    w = self._wait_out_cooldown()
                    if w == "stopped":
                        break
                    if w == "nav_fail":
                        exit_status = "cooldown_nav_fail"
                        break
                    continue
                elif page in ("reward", "end", "back"):
                    # 结算链页面 (宝箱/比赛结束/返回大厅): 本局其实已打完, 只是引擎没等到
                    # "跳过"就先超时了. 直接原地接管结算流程; 旧实现落到下面 else 报
                    # "页面无法识别" -> no_battle_ui 终止, 属冤枉终止 (2026-09-12 收敛).
                    self.log("info", f"识别到已在结算链 ({page}), 直接接管结算流程, 不再重匹配")
                    self._pre_settle_stage = page
                    ret = "battle"      # 复用通用路径: 控制流落到下方结算流程, stage 初值=该页
                elif page in ("session", "login"):
                    # 会话失效 / 停在登录页 (2026-09-13 新增): 关弹窗 -> 重新登录 -> 重导航续跑.
                    rr = self._recover_from_session(f"timeout_{page}")
                    if rr == "recovered":
                        continue
                    exit_status = self._stop_reason("session") if rr == "stop" else "session_nav"
                    break
                elif page == "pve_stage":
                    # 副本关卡页 (引擎绝不该在): 不干等、不终止, 直接按返回键逃生回主城续跑.
                    # 2026-09-12 用户反馈: 落到"地下城里的副本关卡"必报错且不知道怎么退出.
                    self.log("warn", "识别到处于 PvE 副本关卡页 (地下城章节图), "
                                     "按系统返回键逃生回主城")
                    self._save_anomaly("pve_stage_detected")
                    landed = self._escape_unknown_page("pve_stage")
                    if landed is None:
                        exit_status = self._stop_reason("no_battle_ui")
                        break
                    continue
                else:
                    # 完全不认识的页面: 不再直接终止, 先按系统返回键逃生 (最多 4 次),
                    # 退回 主城/主页/战斗页/结算页 任一即可继续本局; 逃生也失败才收尾.
                    # (2026-09-12 用户需求: "进入无法识别的界面就直接 back 退出到主城进行操作")
                    self.log("warn", f"等待比赛界面超时且页面无法识别 ({page}), 尝试按返回键逃生")
                    self._save_anomaly("no_battle_ui_unknown")
                    landed = self._escape_unknown_page("match_timeout")
                    if landed is None:
                        self.log("error", "等待比赛界面超时, 且返回键逃生失败, 任务终止")
                        exit_status = self._stop_reason("no_battle_ui")
                        break
                    if landed == "main_city":
                        self._sleep_stop(2)
                        if not self._enter_pvp_hub_retry("rank"):
                            exit_status = "main_city_nav_fail"
                            break
                    continue
            # ★ 2026-09-22: 能走到这里说明本局已确认进入战斗页 / 结算链 = 有真实进展,
            #   重置"窗口内无进展"计时 —— 否则窗口内自愈的熔断会被正常对局的耗时误伤。
            self._selfheal_progress()
            if self._pre_settle_stage is None:
                if not self._tap_skip_quick():
                    if self.stop_evt.is_set():
                        break
                    self._save_anomaly("skip_fail")
                    exit_status = "skip_fail"
                    break

            # ---- 3-5. 结算流程状态机: 宝箱 -> 比赛结束 -> 返回大厅 (带超时页面识别自愈) ----
            # 任一环节等待超时不再直接异常退出, 而是截图识别当前页面: 认识的页面跳到
            # 对应环节续跑 (2026-09-09 用户实测: 弹窗覆盖跳过按钮致点击未生效, 人停在
            # 战斗页却报"等待宝箱页超时"); 只有完全不认识的页面才存图退出.
            verdict = None
            # 入口 stage: 若本局起点已在结算链某页 (重导航落在宝箱页等), 从该页原地接管,
            # 不再从头等"请选择一个宝箱" (那会白等一轮超时).
            stage = self._pre_settle_stage or "reward"   # reward -> end -> back
            recover_tries = 0
            flow_exit = None        # 非 None = 自愈中发现终止态, 携带原因退出结算流程
            while True:
                if self.stop_evt.is_set():
                    flow_exit = "stopped"
                    break
                if stage == "reward":
                    self._progress("settle", "等待结算+宝箱页")
                    if self._wait_hint(rk.REWARD_HINTS, rk.WAIT_REWARD_SEC, scale=1.0,
                                       desc="等待宝箱选择页", roi=rk.REWARD_ROI):
                        # 结算页标题判定本局胜负 (复用命中帧, 不再多截图)
                        if verdict is None:
                            img = self.last_img
                            if img is not None:
                                verdict = self._judge_rank_result(img)
                                if verdict is not None:
                                    self._apply_rank_verdict(verdict, "结算页")
                        self._tap_third_box()
                        stage = "end"
                        continue
                elif stage == "end":
                    self._progress("settle", "等待比赛结束")
                    hit = self._wait_btn("end_btn.png", rk.END_ROI, rk.END_HINTS,
                                         rk.END_ROI, rk.WAIT_END_SEC, "等待比赛结束")
                    if hit:
                        # 此刻 last_img 正是「开箱奖励」弹窗 (已含奖励清单), 顺手留图+记账
                        self._save_reward_frame(verdict)
                        self._tap(hit["cx"], hit["cy"])
                        self.log("info", f"✓ 点击 比赛结束 @ ({hit['cx']:.0f},{hit['cy']:.0f})")
                        stage = "back"
                        continue
                elif stage == "back":
                    self._progress("settle", "等待返回大厅")
                    hit = self._wait_btn("back_btn.png", rk.BACK_ROI, rk.BACK_HINTS,
                                         rk.BACK_ROI, rk.WAIT_BACK_SEC, "等待返回大厅")
                    if hit:
                        # 结算页没判到胜负时, 用战绩页大字 WIN/LOSE 补判一次 (复用命中帧)
                        if verdict is None:
                            img = self.last_img
                            v2 = self._judge_rank_result(img) if img is not None else None
                            # 2026-09-29 修: 必须回写到 verdict —— 不回写的话, 下面"完成一局"
                            # 的 [战果] 摘要只能拿到 None, 于是打出"第 N 局未判定"却同时
                            # 报"累计 2 胜"的自相矛盾 (胜负明明刚从这行判出来)。
                            verdict = v2
                            self._apply_rank_verdict(v2, "战绩页")
                        self._tap(hit["cx"], hit["cy"])
                        self.log("info", f"✓ 点击 返回大厅 @ ({hit['cx']:.0f},{hit['cy']:.0f})")
                        # 2026-09-13 提速: 标记"下一轮起点确定为主页", 开稳态快车道.
                        self._at_hub_confident = True
                        break   # 结算流程正常完成
                    # (wait 超时落到下方自愈)
                # ---- 当前环节等待超时: 识别页面自愈 ----
                recover_tries += 1
                if recover_tries > 4:
                    self.log("error", f"结算流程自愈超限 (卡在 {stage}), 任务终止")
                    self._save_anomaly(f"settle_loop_{stage}")
                    flow_exit = "no_" + stage
                    break
                page = self._classify_page()
                self.log("warn", f"{stage} 环节等待超时, 页面识别: {page} (自愈 {recover_tries}/4)")
                if page in ("session", "login"):
                    # ★ 会话失效弹窗 / 登录页 (2026-09-13 实测根因): 该弹窗是半透明遮罩,
                    #   战斗页的「跳过」在下面仍可见 -> 若不在此特判, 会掉到下面 page=="battle"
                    #   分支, 变成"重新点跳过" (点击被遮罩吃掉) -> 反复 4 次 -> 自愈超限 ->
                    #   no_reward 终止。实测 12:06~12:10 白烧 4 分钟且 0 场。
                    #   这里重新登录 + 重导航, 并把本局作废回主循环重来 (fought 不增)。
                    rr = self._recover_from_session(f"settle_{stage}")
                    if rr == "recovered":
                        flow_exit = "__retry__"
                    else:
                        flow_exit = (self._stop_reason("session") if rr == "stop"
                                     else "session_nav")
                    break
                if page == "reward":
                    stage = "reward"
                    continue
                if page == "end":
                    self.log("info", "识别到已过宝箱环节 (点击疑被弹窗吞掉), 跳到比赛结束")
                    stage = "end"
                    continue
                if page == "back":
                    self.log("info", "识别到已过结束环节, 跳到返回大厅")
                    stage = "back"
                    continue
                if page == "battle":
                    self.log("warn", "识别到仍在战斗页 (疑弹窗吞掉跳过点击), 重新点跳过")
                    self.last_img = None      # 丢弃命中帧缓存, 强制 _tap_skip_quick 重截图
                    self._tap_skip_quick()
                    stage = "reward"
                    continue
                if page == "hub":
                    self.log("warn", "识别到已在排位主页 (结算被弹窗略过), 本局照常计数, 胜负记未知")
                    if verdict is None:
                        self._apply_rank_verdict(None, "自愈(主页)")
                    break   # 当作本局完成, 回主循环计数
                if page == "main_city":
                    self.log("warn", "识别到被弹回主城, 重新导航回排位主页, 本局照常计数")
                    self._sleep_stop(2)
                    if not self._enter_pvp_hub_retry("rank"):
                        self._save_anomaly("settle_main_city_nav")
                        flow_exit = "main_city_nav_fail"
                    break
                if page == "daily_limit":
                    self.log("error", "自愈识别到每日 30 次上限弹窗, 任务结束")
                    flow_exit = "daily_limit"
                    break
                if page == "cooldown":
                    self.log("warn", "自愈识别到 10 把冷却弹窗, 转冷却处理")
                    if self.cooldown_waits >= rk.MAX_COOLDOWN_WAITS:
                        flow_exit = "cooldown"
                        break
                    w = self._wait_out_cooldown()
                    flow_exit = "stopped" if w == "stopped" else (
                        "cooldown_nav_fail" if w == "nav_fail" else None)
                    break   # 冷却结束/失败都回主循环: 本局已打完, 照常计数
                # unknown / pve_stage: 不认识的页面 (含副本关卡页) -> 先按返回键逃生,
                # 退回流程内页面即继续, 只有逃生也失败才收尾 (2026-09-12 用户需求).
                self.log("warn", f"等待{stage}超时且页面无法识别 ({page}), 尝试按返回键逃生")
                self._save_anomaly(f"unknown_page_{stage}")
                landed = self._escape_unknown_page(f"settle_{stage}")
                if landed is None:
                    self.log("error", f"等待{stage}超时, 且返回键逃生失败, 任务终止")
                    flow_exit = "no_" + stage
                    break
                if landed in ("reward", "end", "back"):
                    self.log("info", f"逃生后回到结算链 ({landed}), 从该环节继续")
                    stage = landed
                    continue
                if landed == "battle":
                    self.log("warn", "逃生后回到战斗页, 重新点跳过并回到宝箱环节")
                    self.last_img = None
                    self._tap_skip_quick()
                    stage = "reward"
                    continue
                if landed == "hub":
                    self.log("warn", "逃生后回到排位主页 (结算环节被略过), 本局照常计数, 胜负记未知")
                    if verdict is None:
                        self._apply_rank_verdict(None, "自愈(逃生->hub)")
                    break
                # main_city: 本局已打完, 重导航后照常计数
                self.log("warn", "逃生后回到主城, 重新导航回排位主页, 本局照常计数")
                self._sleep_stop(2)
                if not self._enter_pvp_hub_retry("rank"):
                    self._save_anomaly("settle_main_city_nav")
                    flow_exit = "main_city_nav_fail"
                    break
                if verdict is None:
                    self._apply_rank_verdict(None, "自愈(逃生->main_city)")
                break
            if flow_exit == "stopped":
                break   # 手动停止: summary 统一归类
            if flow_exit == "__retry__":
                # 会话失效恢复完成: 本局作废 (未打完), 回主循环重新匹配, 不消耗场数
                continue
            if flow_exit:
                exit_status = flow_exit
                break

            # ---- 完成一局, 计数 ----
            self.fought += 1
            # 胜负此时才最终确定 (留图那一刻还不知道) -> 补写回开箱奖励记录
            self._finalize_reward(verdict)
            # 本局走完 = 这一族的自愈成功了: 各族计数归零, 让日志里的"第 N 次"与退避秒数
            # 反映的是**连续**自愈次数, 而不是整个任务期间的累计次数。
            # (2026-09-22: 累计语义下, 一晚每次都被自愈救回也会把计数推到上限 —— 那正是
            #  18:42 被冤枉终止的成因之一; 连续语义才不会误伤正常的长时段挂机。)
            self.match_rescues = 0
            self.nav_rescues = 0
            self.game_rescues = 0
            self.session_rescues = 0
            self.main_city_rescues = 0
            # [战果] 每局一行结构化摘要 (2026-09-29): 用户要的"有用信息" —— 一眼看到
            # 进度/胜负/胜率, 而不是在一堆"✓ 点击 XXX"里自己数。落盘到
            # data/logs/bot-YYYY-MM-DD.log 后可直接 grep "^\[战果\]" 复盘整晚走势。
            _cn = {"win": "胜", "loss": "负"}.get(verdict or "", "未判定")
            _rate = (self.wins / self.fought * 100.0) if self.fought else 0.0
            self.log("ok", f"[战果] 第 {self.fought} 局{_cn} | 累计 {self.fought} 局 "
                           f"{self.wins} 胜 {self.losses} 负, 胜率 {_rate:.0f}%")
            if is_glory:
                self._progress("scan", f"荣耀时刻已完成 {self.fought} 局")
            else:
                self._progress("scan", f"已完成 {self.fought}/{target} 局")
            self._sleep_stop(1)  # 缓冲回主页稳定 (原 2s)

        return self._summary(exit_status, started)

    # ================================================================ 任务主循环
    def _pick_target(self, img) -> tuple | None:
        """从当前列表挑第一个值得打的人 -> (name, btn); 无则 None。"""
        from sj_bot.vision import scan_opponent_list
        opps = scan_opponent_list(img)
        if not opps:
            return None
        for opp in opps:
            if opp.name in self._stale_names:
                # 本任务内反复点击进不了战斗的行 (OCR 显示挑战但实际不可挑战),
                # 跳过让选择器向下走 —— 否则永远撞第一行 (2026-09-13 首跑教训)。
                self.log("info", f"跳过(本任务拉黑: 点不动): {opp.name}")
                continue
            act = self.flow.decide({"name": opp.name}, use_cap=(self.mode == "arena"))
            if act == "stop":
                self.log("ok", "周积分已达 6000 上限, 本周收手")
                return ("__stop__", (0, 0))
            if act == "battle":
                return (opp.name, opp.btn)
            if act == "skip":
                self.log("info", f"跳过(打不过名单): {opp.name}")
            elif act == "cooldown":
                self.log("info", f"跳过(10分钟冷却): {opp.name}")
        return None

    def run(self) -> dict:
        started = _dt.datetime.now()
        consecutive_fail = 0   # 连续截图失败次数(超限终止)
        lost_fail = 0          # 连续不在对战列表次数(超限终止)
        exit_status = "finished"  # arena 循环收尾状态; rank/rank_glory 分支在各自方法内维护
        if self.mode == "rank":
            return self._run_rank(started)
        if self.mode == "rank_glory":
            return self._run_rank(started, glory=True)
        try:
            if self.mode == "arena":
                if not in_window():
                    self.log("warn", "当前不在全民争霸时段(周六/日 16:00-17:00), 任务直接结束")
                    return self._summary("not_in_window", started)
                self.log("info", f"全民争霸自动赛开始 (周积分 {self._db.weekly_win_points()}/{WEEKLY_POINTS_CAP}, "
                                 f"行动力机制: 有行动力连打/耗尽后每 {STAMINA_RECOVER_SEC}s 一场)")
                if not self._enter_pvp_hub_retry("arena"):
                    return self._summary("nav_failed", started)
            else:
                n = self.rounds or 0
                self.log("info", f"排位赛自动打开始: 目标 {n} 场 (每场间隔 {self.interval}s)")
                if n <= 0:
                    self.log("error", "排位赛需要指定场数(rounds>=1)")
                    return self._summary("bad_rounds", started)

            while not self.stop_evt.is_set():
                # 截图失败/不在列表 的连续失败守卫: 成功一次即归零
                if self.mode == "rank" and self.fought >= (self.rounds or 0):
                    self.log("ok", f"排位赛完成: 已打满 {self.fought} 场")
                    break
                # 每轮开打前也检查 arena 时段/上限
                if self.mode == "arena" and not in_window():
                    self.log("warn", "全民争霸窗口已结束(非周六/日 16-17), 任务结束")
                    break
                if self.mode == "arena" and self._db.weekly_win_points() >= WEEKLY_POINTS_CAP:
                    self.log("ok", "周积分达上限, 自动收手")
                    break

                img = self._shot_img()
                if img is None:
                    self.log("error", "截图失败(模拟器离线?), 5s 后重试")
                    consecutive_fail += 1
                    if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                        self.log("error", "连续截图失败次数过多, 任务终止(请检查模拟器连接)")
                        break
                    self._sleep_stop(5)
                    continue
                consecutive_fail = 0

                # 0.5) 战斗页接管 (2026-09-13): 任务停止/重启可能残留一局进行中的战斗
                #      (导航已放行"已在战斗页"), 但本循环原来只有"不在列表"一条路 ->
                #      6 次重试后误判"长时间不在列表"终止 (16:41 首跑复现)。改为:
                #      等可跳过就点跳过, 结算出现就点关闭, 不计 lost_fail。
                #      胜负由游戏自动战斗决定, 不写档案 (无对手名上下文)。
                if self._is_in_battle(img):
                    self.log("info", "arena 主循环: 检测到战斗页, 接管跳过与结算")
                    self._sleep_stop(10)
                    bimg = self._shot_img()
                    if bimg is not None and self._is_in_battle(bimg):
                        hit = self._find_skip_btn(bimg)
                        if hit:
                            self._tap(hit["cx"], hit["cy"])
                            self.log("info", "✓ 点击 跳过 (残留战斗接管)")
                        self._sleep_stop(8)
                    dimg = self._shot_img()
                    if dimg is not None:
                        d = self._find_hint(dimg, DISMISS_HINTS,
                                            roi=(300, 500, 1300, 860), scale=1.5)
                        if d:
                            self._tap(d["cx"], d["cy"])
                            self.log("info", "✓ 点击结算关闭")
                    self._sleep_stop(3)
                    continue

                if not self._is_on_list(img):
                    lost_fail += 1
                    self.log("warn", f"当前画面不在对战列表(第 {lost_fail} 次), 等待/重扫")
                    if lost_fail == 1:
                        self._save_anomaly("not_list")
                    if lost_fail >= MAX_CONSECUTIVE_FAIL:
                        # 2026-09-22 用户口径: "到指定排位时间了就要进入排位界面" ——
                        # 窗口内长时间不在列表不再直接收工, 先重导航回对战列表续跑;
                        # 窗口已关 / 窗口内长时间无进展 才真的终止 (见 _selfheal_gate)。
                        if not self._selfheal_ok("nav_rescues", "长时间不在对战列表页"):
                            self._save_anomaly("not_list_giveup")
                            exit_status = self._stop_reason("not_list")
                            break
                        self._selfheal_pause(self.nav_rescues)
                        self.log("warn", "重新导航回全民争霸对战列表后继续")
                        if not self._enter_pvp_hub_retry("arena"):
                            exit_status = "nav_failed"
                            break
                        lost_fail = 0
                        continue
                    self._sleep_stop(SCAN_EMPTY_RETRY)
                    continue
                lost_fail = 0

                target = self._pick_target(img)
                if target is None:
                    # arena: 名单实时刷新, 缩短重扫间隔抢「修养结束」后的第一次出手
                    retry = ARENA_EMPTY_RETRY if self.mode == "arena" else SCAN_EMPTY_RETRY
                    self.log("info", f"当前列表没有可打目标(全跳过/修养中/无人), "
                                     f"{retry}s 后重扫")
                    self._progress("scan", "无可打目标, 重扫中")
                    self._sleep_stop(retry)
                    continue
                if target[0] == "__stop__":
                    break
                name, btn = target
                st = self._do_one_battle(name, btn)
                if st == "fatal":
                    # 卡在未知界面, 直接终止(不自动乱点)
                    break
                if st in ("stale", "stale_pre"):
                    # 2026-09-13: 无弹窗阻断 = 目标行翻回「修养中」(秒级翻转, 见
                    # _do_one_battle 内注释), 短等重扫即可; 与行动力 60s 等待区分。
                    # stale (点击后) => 该对手记入本任务拉黑名单: 重扫后 _pick_target
                    # 不再选它, 否则会无限撞同一行 (16:52~16:59 雪人夜樱花 5 连阻断)。
                    # stale_pre (点击前复核) => **不拉黑**: 行秒级翻回, 是好目标, 且
                    # 复核失败成本仅 ~2s (选人+复核), 无 5 连阻断的 35s 代价。
                    # 2026-09-17: stale_pre 不再等 8s —— 该行要修养 ~30s, 但名单实时刷新,
                    # 立刻重扫就能抢**别的**可打的人 (用户口径: 快速点击能打的人)。
                    if st == "stale":
                        self._stale_names.add(name)
                    self._stale_streak = getattr(self, "_stale_streak", 0) + 1
                    if self._stale_streak >= MAX_BLOCK_STREAK:
                        self.log("error", f"连续 {self._stale_streak} 次点击时目标行已翻回修养中"
                                          f"(列表行状态竞态), 终止任务; 现场图见 captures/")
                        exit_status = "stale_list"
                        break
                    wait = STALE_RETRY_SEC if st == "stale" else STALE_PRE_RETRY_SEC
                    self._progress("scan", "目标行翻回修养中, 短等重扫")
                    self.log("info", f"目标行已翻回修养中, {wait:.0f}s 后重扫找下一个可打的人 "
                                     f"(第 {self._stale_streak}/{MAX_BLOCK_STREAK} 次)")
                    self._sleep_stop(wait)
                    continue
                if st == "blocked":
                    # 2026-09-11 新规: 行动力耗尽时点挑战不会进战斗页 -> 等 1 分钟
                    # 恢复 1 点行动力再试 (即"一分钟一把"), 不是错误, 不计异常。
                    self.stamina_waits += 1
                    if self.stamina_waits >= MAX_BLOCK_STREAK:
                        self.log("error", f"连续 {self.stamina_waits} 次点挑战均未进入战斗页"
                                          f"(行动力不足或界面异常), 终止任务; 现场图见 captures/")
                        exit_status = "no_stamina"
                        break
                    self._progress("stamina", f"行动力不足, 等 {STAMINA_RECOVER_SEC}s 恢复")
                    self.log("info", f"等 {STAMINA_RECOVER_SEC}s 恢复 1 点行动力后重试 "
                                     f"(第 {self.stamina_waits}/{MAX_BLOCK_STREAK} 次)")
                    self._sleep_stop(STAMINA_RECOVER_SEC)
                    continue
                self.stamina_waits = 0   # 成功打完一场即清零
                self._stale_streak = 0   # 同上, 行翻回修养中的连击计数也清零
                self.nav_rescues = 0     # 2026-09-22: 重导航自愈也按"连续"计
                lost_fail = 0
                self._progress("scan", f"已完成 {self.fought} 场")
        finally:
            pass
        return self._summary(exit_status, started)

    def _summary(self, reason: str, started: _dt.datetime) -> dict:
        # 手动停止兜底: stop_evt 已置位且非"天然收尾"原因(窗口结束/冷却/参数错误等) ->
        # 一律归类 stopped, 避免"手动停止却显示 正常完成/超时"的误导
        if self.stop_evt.is_set() and reason not in (
                "stopped", "glory_done", "not_in_window", "not_glory_window",
                "cooldown", "bad_rounds", "daily_limit", "no_stamina", "stale_list"):
            if reason != "finished":
                self.log("warn", f"收到停止请求, 原因 {reason} 归类为手动停止")
            reason = "stopped"
        self._progress("done", reason)
        self.log("info", f"任务结束({reason}): 打 {self.fought} 场 = 胜 {self.wins} / "
                         f"负 {self.losses} / 跳过 {self.skips} / 异常 {self.errors} / "
                         f"等行动力 {self.stamina_waits} 次")
        return {
            "mode": self.mode, "reason": reason, "fought": self.fought,
            "wins": self.wins, "losses": self.losses, "skips": self.skips,
            "errors": self.errors, "points": self._db.weekly_win_points(),
            "stamina_waits": self.stamina_waits,
            "started_at": started.isoformat(timespec="seconds"),
        }

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass
