"""决策状态机 —— 规则已落定 (2026-09-07 初版 / 2026-09-11 按新版游戏规则修订)。

规则 (来自游戏"全民争霸"说明 1-3 点 + 用户口径):
  R1 比赛仅每周六/周日 16:00-17:00 开启 -> 主循环时段闸门 in_window();
     (2026-09-11 实测修订: 面板写 16:00-17:00, 页面倒计时"距离活动开始时间"
      指向当日 16:00:00 整, 原 15:00 作废)
  R2 每胜 +80 兑换积分, 周上限 6000 -> 75 胜后本周收手 (weekly_win_points);
     (2026-09-11 修订: 面板"单场挑战胜利可获得80积分"; 名人模式下主动挑战
      非名人玩家胜利积分减半(40), 故本计数是保守上限, 不是精确游戏内积分)
  R3 (2026-09-11 作废旧"30s 单场 CD") 行动力机制:
     每次挑战消耗 1 点行动力; 初始 10 点; 每分钟自动恢复 1 点; 上限 20 点。
     -> 有行动力时可连续挑战(单场间隔 0); 行动力耗尽后每分钟只能打 1 场
        (STAMINA_RECOVER_SEC=60)。
  R4 (2026-09-17 按用户口述修订, 原"同一玩家 10 分钟不可重复攻击"作废)
     限制在**被打的人**身上, 不在自己身上: 某人一旦被(任何人)挑战, 立即进入
     「修养中」约 30s, 期间列表上该行的「挑战」按钮消失, 不可再被挑战;
     列表**实时刷新** (谁被打、谁可打 都在变)。
     -> 本地只按 ATTACK_COOLDOWN_SEC=30 做软过滤; **权威判据是屏幕状态**
        (扫到「挑战」短块才算可打 / 点击前 _btn_still_challenge 复核),
        因为别人打的场次我们本地根本不知道。
  R5 纯黑名单决策 (2026-09-08 用户确认, 取代旧"能打名单"模型):
       - verdict == 'no'(打不过名单) -> 本次窗口内一直跳过
       - 其余一律 battle: unknown 陌生首战攒档案、beat 曾战胜、
         以及首败观察中(连败 < 2)的对手
       - "打不过"由 db.record 维护: 实战连败 >= 2 次才定性 no;
         skip(排行采集预判)仅对从未战胜者生效
       - beat 只是战绩标签, 不参与决策、不导出

decide() 只做"对列表里一个对手的决策", 返回:
  'battle' / 'skip' / 'stop' (本周积分达上限) / 'cooldown'(对手修养中 30s)
主循环据此驱动视觉动作; 视觉动作(截图/OCR/点击)在 vision 层接。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum

from .device import Device
from .db import OpponentDB, POINTS_PER_WIN

# 规则参数 (可从配置文件/环境变量覆盖)
WEEKEND_ONLY = True          # R1: 是否只在周六/日开放
OPEN_WEEKDAYS = (5, 6)       # 周六=5, 周日=6
OPEN_START_HOUR = 16         # 16:00 (2026-09-11 修订, 原 15:00)
OPEN_END_HOUR = 17           # 17:00 (不含)
ATTACK_COOLDOWN_SEC = 30     # R4: 对手「修养中」窗口 (2026-09-17 用户口述: 被打后 ~30s 不可再被挑战)
# R2: 每胜积分(80) 与每周上限 —— POINTS_PER_WIN 的唯一口径在 db.py, 此处直接引用
WEEKLY_POINTS_CAP = 6000     # R2: 每周积分上限(单周兑换币转化上限)

# ---- R3 (2026-09-11 新规): 行动力机制, 取代旧"30s 单场 CD" ----
ARENA_INTERVAL_SEC = 0       # 有行动力: 连打, 两场之间不等待
BATTLE_INTERVAL_SEC = ARENA_INTERVAL_SEC  # 兼容旧名(自 2026-09-11 起为 0)
STAMINA_COST_PER_BATTLE = 1  # 每次挑战消耗 1 点行动力
STAMINA_INITIAL = 10         # 活动初始行动力
STAMINA_MAX = 20             # 行动力存储上限
STAMINA_RECOVER_SEC = 60     # 每分钟自动恢复 1 点 -> 耗尽后 1 场/分钟
NAME_NORMALIZE_MAP = {"绯": "绯"}  # OCR 纠错预留(名字近似归一)

# ---- 荣耀时刻 (排位赛, 每日) ----
GLORY_START_HOUR = 18        # 每晚 18:00
GLORY_END_HOUR   = 20        # 每晚 20:00 (不含)
GLORY_INTERVAL_SEC = 120     # 荣耀时刻每场 2 分钟准备 (规则第 7 条)


def in_glory_window(now: _dt.datetime | None = None) -> bool:
    """荣耀时刻闸门: 每晚 18:00-20:00 (每天开放, 无每日场次上限)。"""
    now = now or _dt.datetime.now()
    return GLORY_START_HOUR <= now.hour < GLORY_END_HOUR


def next_glory_start(now: _dt.datetime | None = None) -> _dt.datetime:
    """下一个荣耀时刻窗口开始时刻 (今天 18:00 未到即今天, 否则明天)。"""
    now = now or _dt.datetime.now()
    start = now.replace(hour=GLORY_START_HOUR, minute=0, second=0, microsecond=0)
    if start <= now:
        start += _dt.timedelta(days=1)
    return start


def next_glory_end(now: _dt.datetime | None = None) -> _dt.datetime:
    """当前/下一个荣耀时刻窗口的结束时刻 (今天 20:00 未过即今天)。"""
    now = now or _dt.datetime.now()
    end = now.replace(hour=GLORY_END_HOUR, minute=0, second=0, microsecond=0)
    if end <= now:
        end += _dt.timedelta(days=1)
    return end


def window_state(jtype: str, now: _dt.datetime | None = None
                 ) -> tuple[bool, _dt.datetime | None]:
    """任务类型的"时段闸门"统一查询 —— 预约启动调度器与 API 共用同一口径。

    返回 (当前是否可启动, 下次可启动时刻; None 表示已开放/无时段限制)。
      arena      : 周六/日 16:00-17:00 (in_window)
      rank_glory : 每晚 18:00-20:00
      rank       : 随时, 但 18-20 被荣耀时刻占用 (系统自动参赛, 点匹配无效)
      collect    : 无时段限制
    """
    now = now or _dt.datetime.now()
    if jtype == "arena":
        if in_window(now):
            return True, None
        return False, next_open_start(now)
    if jtype == "rank_glory":
        if in_glory_window(now):
            return True, None
        return False, next_glory_start(now)
    if jtype == "rank":
        if in_glory_window(now):
            return False, next_glory_end(now)
        return True, None
    return True, None


class State(str, Enum):
    SCAN = "scan_list"        # 扫描当前对手列表
    EVAL = "evaluate"         # 逐个读档案评估可打性
    BATTLE = "battle"         # 开战与等待结算
    SETTLE = "settle"         # 结算识别并回写档案
    IDLE = "idle"             # 无任务/暂停


@dataclass
class BattleContext:
    """单局处理的上下文, 由主循环推进。"""

    state: State = State.IDLE
    current_opponent: dict = field(default_factory=dict)
    stop: bool = False
    log: list[str] = field(default_factory=list)


def in_window(now: _dt.datetime | None = None) -> bool:
    """R1: 时段闸门。默认仅周六/周日 16:00-17:00 返回 True (2026-09-11 修订)。"""
    now = now or _dt.datetime.now()
    if WEEKEND_ONLY and now.weekday() not in OPEN_WEEKDAYS:
        return False
    return OPEN_START_HOUR <= now.hour < OPEN_END_HOUR


def next_open_start(now: _dt.datetime | None = None) -> _dt.datetime:
    """从 now 起下一个开放窗口的开始时间 (用于主循环 sleep 到点)。"""
    now = now or _dt.datetime.now()
    for d in range(0, 8):
        t = now + _dt.timedelta(days=d)
        if WEEKEND_ONLY and t.weekday() not in OPEN_WEEKDAYS:
            continue
        start = t.replace(hour=OPEN_START_HOUR, minute=0, second=0, microsecond=0)
        if start > now:
            return start
    raise RuntimeError("无法计算下一个开放窗口")  # 理论不可达


class GameFlow:
    """对局主循环骨架。规则已落定; 视觉动作待 vision 层对接。"""

    def __init__(self, device: Device, db: OpponentDB) -> None:
        self.dev = device
        self.db = db
        self.ctx = BattleContext()

    # ---------- 判定核心 (纯逻辑, 可单测) ----------
    def decide(self, opp: dict, use_cap: bool = True) -> str:
        """对列表中的一个对手做出决策。

        opp 来自视觉层扫描结果, 至少含 name; 可选 rating/level。
        use_cap=False 时跳过周积分上限检查(排位赛等非全民争霸任务用)。
        返回: 'battle' | 'skip' | 'stop' | 'cooldown'
          battle   - 开打 (不在打不过名单: 陌生首战 / 曾战胜 / 首败观察中)
          skip     - 打不过名单, 本次窗口不再尝试
          cooldown - 本任务内刚打过, 30s「修养中」未满, 暂不可重复攻击
                     (软过滤; 真权威是列表上该行有没有「挑战」按钮)
          stop     - 本周积分已达上限, 收手
        """
        if use_cap and self.db.weekly_win_points() >= WEEKLY_POINTS_CAP:
            return "stop"

        rec = self.db.get(opp["name"])
        if rec is None:
            # 精确未命中时再走模糊匹配 (OCR 艺术字近字错字很常见, 如 绯->排):
            # 否则"打不过名单"会因一个字之差被静默绕过 —— 名单形同虚设。
            # 只在精确未命中时启用, 不改变已有精确命中的行为。
            rec = self.db.find_similar(opp["name"])
        verdict = rec["verdict"] if rec else "unknown"

        if verdict == "no":
            return "skip"  # 打不过名单(唯一闸门): 本次窗口不再尝试
        # 其余(beat 战绩 / unknown 陌生 / 首败观察中)都开打;
        # 但刚打过的同一人还在「修养中」(30s) 时不可重复攻击
        if self.db.is_on_cooldown(opp["name"], ATTACK_COOLDOWN_SEC):
            return "cooldown"
        return "battle"

    def on_battle_start(self, name: str) -> None:
        """确认进入战斗页后调用: 记录攻击时刻 (对手进入 30s「修养中」)。"""
        self.db.mark_attack(name)

    def on_settle(self, name: str, result: str,
                  rating: int | None = None, level: int | None = None) -> None:
        """结算识别完成后调用: 回写胜负档案。result: 'win'/'loss'/'skip'。"""
        self.db.record(name, result, rating=rating, level=level)

    def should_continue(self) -> bool:
        """主循环是否继续: 窗口开着且本周未到上限。"""
        return in_window() and not self.ctx.stop

    # ---------- 主循环骨架 (视觉动作留待 vision 层) ----------
    def run(self) -> None:
        """占位主循环。结构:
        while self.should_continue():
            for opp in scan_list():        # vision: 读对手列表
                act = self.decide(opp)
                if act == 'stop':   self.ctx.stop = True; break
                if act == 'skip':   continue           # 下一个对手
                if act == 'cooldown': continue         # 下一个对手
                # battle: 点击挑战 -> 等待战斗 -> 识别胜负 -> 回写
        """
        raise NotImplementedError(
            "主循环视觉动作待 vision 层(截图/OCR/点击)对接后实现"
        )
