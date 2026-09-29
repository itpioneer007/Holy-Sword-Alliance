"""圣剑挂机 Web 控制台 (Flask) —— 任务操作台版。

启动: python -m sj_bot.server   ->  http://127.0.0.1:5050
核心思路: 页面只管"点按钮下发任务", 后端 JobManager 在后台线程自动执行
  (全民争霸自动赛 / 排位赛按场数 / 荣耀排行采集), 互斥单任务, 页面实时看进度与日志。

API:
  页面/状态: GET /            GET /api/status   (含 schedule: 待执行的预约)
  任务:      POST /api/jobs/start {type: arena|rank|rank_glory|collect, rounds?, pages?}
             POST /api/jobs/stop        GET /api/jobs/status
  预约(2026-09-22 新增): POST /api/jobs/schedule {type, when:"18:00"|"90"|"30s", params?}
             POST /api/jobs/schedule/cancel {type?}   GET /api/jobs/schedule
             —— "人不在, 到点自动开打": 服务端线程到点自动下发, 页面关了也生效, 落盘跨重启
  调试(保留): GET /api/shot  POST /api/tap  POST /api/swipe  POST /api/ocr
  档案:      GET /api/db/stats  GET /api/db/names?verdict=&limit=
  日志:      GET /api/logs?after=
兼容旧端点: /api/collect/start|status|stop -> jobs(type=collect)
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, send_file, send_from_directory

from sj_bot.botlog import BotLog
from sj_bot.battler import Battler
from sj_bot.collector import RankCollector
from sj_bot.config import Config
from sj_bot.db import OpponentDB

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
cfg = Config()
LOG = BotLog(file_dir=cfg.data_dir / "logs")   # 按日落盘 (09-29): 进程重启不再失忆
_log = LOG  # 兼容别名

JOB_CN = {"arena": "全民争霸", "rank": "日常排位",
          "rank_glory": "荣耀时刻挂机", "collect": "排行采集"}

# ================================================================ 任务终止原因 (唯一口径)
# 2026-09-22 用户需求: "终止原因也要一眼就能看到"。
# 引擎的 exit_status(reason) 是**内部分类码** (battler.py 各收尾分支), 直接呈现给用户
# 就是一句 "no_battle_ui" 天书。这里做唯一一份规范化: 中文说明 + 是否算异常 + 该做什么。
# 前端只负责渲染, 不再各自维护词表 (旧前端保留本地兜底, 兼容浏览器缓存)。
#   abnormal=True  -> 需要人看一眼 (红条)
#   abnormal=False -> 正常收尾 (灰/绿条)
JOB_REASON: dict[str, tuple[str, bool, str]] = {
    # ---------------- 正常收尾 ----------------
    "finished":         ("已打满目标场数", False, ""),
    "stopped":          ("手动停止", False, ""),
    "glory_done":       ("荣耀时刻窗口(18-20)结束, 正常收尾", False, ""),
    "not_in_window":    ("已不在全民争霸时段(周六/日 16-17)", False, ""),
    "not_glory_window": ("已不在荣耀时刻时段(18-20)", False, ""),
    "daily_limit":      ("今日累计 30 次挑战已满", False,
                         "18-20 荣耀时刻不受此限; 明日恢复"),
    "bad_rounds":       ("启动参数不合法", True, "排位赛场数需 ≥1"),
    "cooldown":         ("日常排位 10 把后进入冷却, 等待次数超限", True,
                         "冷却 15 分钟即可继续; 或用荣耀时刻挂机避开"),
    "cooldown_nav_fail": ("冷却等待结束后重导航失败", True, "检查游戏是否停在登录页/主城异常"),
    # ---------------- 窗口内自愈收尾 (2026-09-22 新增口径) ----------------
    "window_closed":    ("任务时段已结束, 停止重试", False,
                         "下一时段可用「预约启动」自动开打, 无需守着"),
    "selfheal_stall":   ("窗口内长时间没能打成一局, 自愈已尽力", True,
                         "多为模拟器内存不足导致游戏进程被回收: 关掉占内存的程序, "
                         "或在 MuMu 设置里给虚拟机多分内存"),
    "selfheal_cap":     ("自愈次数达上限", True, "查看 captures/ 现场图定位"),
    # ---------------- 需要人工看一眼 ----------------
    "nav_failed":       ("导航失败, 进不去排位入口", True, "检查游戏是否停在登录页/主城异常"),
    "nav_fail":         ("逃生/重导航失败", True, "同上"),
    "not_list":         ("长时间不在全民争霸对战列表", True, "查看 captures/not_list*.png"),
    "not_rank_hub":     ("既不在排位主页也不在战斗页", True, "查看 captures/not_rank_hub*.png"),
    "no_battle_ui":     ("等不到比赛界面(未见跳过按钮)", True,
                         "看 captures/ 现场图; 若同时出现 game_gone/session 日志, "
                         "根因是模拟器内存不足回收了游戏"),
    "no_battle_ui_after_antibot": ("防挂机处理后仍等不到比赛界面", True, ""),
    "match_rescue_loop": ("匹配等待自愈次数超限", True, ""),
    "main_city_loop":   ("反复被弹回主城", True, "多为防挂机弹窗期间匹配倒计时归零"),
    "main_city_nav_fail": ("被弹回主城后重导航失败", True, ""),
    "session":          ("游戏会话失效且未能自动恢复", True,
                         "在模拟器里手动登录一次游戏即可"),
    "session_nav":      ("重新登录后导航失败", True, "同上"),
    "game_gone":        ("游戏进程被回收且未能拉起", True,
                         "多半是模拟器内存不足: 关掉占内存的程序"),
    "game_gone_nav":    ("游戏拉起后导航失败", True, ""),
    "antibot_kicked_nav": ("防挂机答错被踢回主城后导航失败", True, ""),
    "antibot_giveup":   ("防挂机验证未通过", True, ""),
    "antibot_retry":    ("防挂机截图失败", True, ""),
    "antibot_no_llm":   ("防挂机本地解题失败且模型不可用", True, ""),
    "single_match_fail": ("点击单人匹配失败", True, ""),
    "skip_fail":        ("点击跳过失败", True, ""),
    "box_fail":         ("点击第三宝箱失败", True, ""),
    "no_reward":        ("等不到宝箱选择页", True, ""),
    "no_end":           ("等不到比赛结束", True, ""),
    "no_back":          ("等不到返回大厅", True, ""),
    "no_stamina":       ("连续点挑战均未进战斗(行动力不足或界面异常)", True, "查看 captures/"),
    "stale_list":       ("目标行反复翻回修养中(列表状态竞态)", True, ""),
}


def reason_info(code: Optional[str]) -> dict:
    """内部收尾码 -> {code, cn, abnormal, hint, known}。未知码原样回显并标为异常。"""
    if not code:
        return {"code": "", "cn": "未知原因", "abnormal": True,
                "hint": "查看日志与 captures/ 现场图", "known": False}
    hit = JOB_REASON.get(code)
    if hit is None:
        return {"code": code, "cn": code, "abnormal": True, "known": False,
                "hint": "引擎新增了未登记的收尾码, 请补进 server.JOB_REASON"}
    cn, abnormal, hint = hit
    return {"code": code, "cn": cn, "abnormal": abnormal, "hint": hint, "known": True}


def enrich_result(result: Optional[dict]) -> Optional[dict]:
    """给任务结果补上"人话版"终止原因 (不改引擎侧数据结构, 只加派生字段)。"""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    info = reason_info(result.get("reason"))
    out["reason_cn"] = info["cn"]
    out["abnormal"] = info["abnormal"]
    out["reason_hint"] = info["hint"]
    out["reason_known"] = info["known"]
    return out


app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")


@app.after_request
def _no_store(resp):
    """页面/API 一律禁止缓存: 浏览器长期开着页面期间服务端若更新过前端,
    强缓存会拿到旧 HTML+旧 JS, 造成"日志不刷新/旧元素缺失 TypeError/黑屏"类怪问题.
    开发控制台无 CDN 场景, no-store 代价可忽略."""
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ---- 设备层: 直接走 adb (不依赖 uiautomator2 session, 更轻) ----
SERIAL = cfg.serial or "127.0.0.1:16384"
ADB = cfg.adb_exe


def _adb(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    if args[:1] == ["connect"]:
        cmd = [ADB, "connect", SERIAL]
    else:
        cmd = [ADB, "-s", SERIAL, *args]
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _shot() -> bytes | None:
    # MuMu 的 adb transport 空闲易断, 每次截图前先 connect
    try:
        subprocess.run([ADB, "connect", SERIAL], capture_output=True, timeout=8)
    except Exception:
        pass
    r = _adb(["exec-out", "screencap", "-p"])
    return r.stdout if r.returncode == 0 and r.stdout else None


# ================================================================ 任务管理器
class JobHistory:
    """任务完成记录: 追加写 data/job_history.jsonl (跨重启保留), 内存只读最近 N 条。"""

    def __init__(self, path: Path, maxlen: int = 30) -> None:
        self.path = Path(path)
        self.maxlen = maxlen
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def add(self, rec: dict) -> None:
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                lines = self.path.read_text(encoding="utf-8").splitlines()
                if len(lines) > self.maxlen * 2:
                    self.path.write_text(
                        "\n".join(lines[-self.maxlen:]) + "\n", encoding="utf-8")
            except Exception:
                pass

    def recent(self, n: int = 12) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        try:
                            out.append(json.loads(ln))
                        except Exception:
                            pass
        except FileNotFoundError:
            return []
        except Exception:
            return []
        return out[-n:]


class JobManager:
    """单任务互斥调度: 同一时刻只允许一个后台任务 (对局/采集) 运行。
    任务结束的槽位保留 (供前端读结果), 仅在发起新任务前清除; 完成记录写入 history。"""

    def __init__(self, log: BotLog, history: Optional[JobHistory] = None) -> None:
        self._log = log
        self._history = history
        self._lock = threading.Lock()
        self._job: Optional[dict] = None

    def running(self) -> bool:
        with self._lock:
            return self._job is not None and self._job["state"] == "running"

    def start(self, jtype: str, params: dict, fn) -> Optional[str]:
        """注册一个任务。fn(evt) 在工作线程执行。已在跑则返回错误文案。"""
        with self._lock:
            if self._job is not None:
                return "已有任务在运行, 请先停止当前任务"
            evt = threading.Event()
            self._job = {
                "type": jtype, "params": params, "evt": evt,
                "state": "running", "detail": {}, "result": None,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": None,
                "stop_requested": False,
            }
            thread = threading.Thread(
                target=self._wrap, args=(jtype, evt, fn), daemon=True, name=f"job-{jtype}"
            )
            thread.start()
        self._log.info(f"任务启动: {jtype} {params}")
        return None

    def _wrap(self, jtype: str, evt: threading.Event, fn) -> None:
        t0 = time.time()
        state, err, result = "done", None, None
        try:
            result = fn(evt)
        except Exception as e:
            self._log.error(f"任务异常: {type(e).__name__}: {e}")
            state, err = "error", f"{type(e).__name__}: {e}"
        with self._lock:
            job = self._job
            if job:
                job["state"] = state
                job["error"] = err
                job["result"] = result
                job["detail"] = {}
        # 任务完成 -> 写历史 (含耗时), 供页面"最近任务"回看
        if self._history is not None and job is not None:
            self._history.add({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": jtype,
                "params": job.get("params") or {},
                "state": state,
                "result": result,
                "error": err,
                "elapsed": round(time.time() - t0, 1),
            })

    def stop(self) -> None:
        with self._lock:
            job = self._job
        if job:
            with self._lock:
                job["stop_requested"] = True
            job["evt"].set()
            self._log.warn("请求停止任务(当前步骤完成后退出)")

    def status(self) -> dict:
        with self._lock:
            job = self._job
        if job is None:
            return {"running": False, "stopping": False}
        running = job["state"] == "running"
        return {
            "running": running,
            "stopping": running and bool(job.get("stop_requested")),
            "type": job["type"],
            "params": job["params"],
            "state": job["state"],
            "started_at": job["started_at"],
            "error": job["error"],
            "detail": job["detail"] or {},
            "result": job["result"],
        }

    def clear_if_done(self) -> None:
        """任务结束后槽位保留(供前端读结果), 仅在发起新任务前清除。"""
        with self._lock:
            if self._job and self._job["state"] in ("done", "error"):
                self._job = None


JOBS = JobManager(LOG, JobHistory(cfg.data_dir / "job_history.jsonl"))


def _run_battle(evt: threading.Event, jtype: str, params: dict) -> dict:
    """对局任务线程入口 (arena / rank / rank_glory)。进度通过 LOG 与 detail 上报。"""
    battler = Battler(
        mode=jtype,
        rounds=params.get("rounds"),
        log=lambda lvl, msg: LOG.append(lvl, msg),
        stop_evt=evt,
        on_progress=lambda p: _update_detail(p),
    )
    try:
        return battler.run()
    finally:
        battler.close()


def _update_detail(p: dict) -> None:
    with JOBS._lock:
        if JOBS._job:
            JOBS._job["detail"] = p


def _run_collect(evt: threading.Event, _params: dict) -> dict:
    pages = int(_params.get("pages", 40))
    col = RankCollector(pages=pages, log=lambda lvl, m: LOG.append(lvl, m), stop_evt=evt)
    return col.run()


# ---------------------------------------------------------------- 下发任务的公共口径
def _norm_params(jtype: str, params: dict) -> tuple[Optional[dict], Optional[str]]:
    """参数归一 + 钳制。手动点击与预约自动触发**共用同一套口径**,
    避免"预约路径绕过每日 30 把上限"这类两条路各写一份校验导致的漂移。
    返回 (params, 错误文案)。"""
    params = params or {}
    try:
        rounds = max(0, int(params.get("rounds", 0)))
        pages = min(int(params.get("pages", 40)), 400)
    except (TypeError, ValueError):
        return None, "rounds/pages 需为数字"
    if jtype == "rank" and rounds <= 0:
        return None, "排位赛请先选择要打的场数(>=1)"
    # 每日 30 次累计上限 (2026-09-09 实测弹窗): 超出的场数反正打不了, 直接钳制而非拒绝,
    # 避免用户输入 40 只得到一个报错; 引擎侧另有弹窗检测兜底 (daily_limit 收尾)
    if jtype == "rank" and rounds > 30:
        from sj_bot.rank_layout import RANK_MAX_ROUNDS_PER_DAY as cap
        rounds = cap
        print(f"[jobs] rounds 超出每日上限, 已钳制为 {cap}")
    if jtype in ("arena", "rank", "rank_glory"):
        return {"rounds": rounds}, None
    return {"pages": pages}, None


def _window_error(jtype: str) -> Optional[str]:
    """时段互斥校验: 返回拒启文案或 None。文案与历史版本逐字一致(前端/习惯依赖)。"""
    from sj_bot.state_machine import in_glory_window
    if jtype == "rank" and in_glory_window():
        return ("荣耀时刻进行中(18:00-20:00), 系统自动参赛中——日常排位已禁用, "
                "请改用 type=rank_glory (荣耀时刻挂机) 模式")
    if jtype == "rank_glory" and not in_glory_window():
        return ("荣耀时刻仅在每晚 18:00-20:00 开放, 当前未到窗口——"
                "日常排位请用 type=rank 模式")
    return None


def _dispatch_job(jtype: str, params: dict) -> Optional[str]:
    """统一的"下发任务"入口 —— 手动点击与预约自动触发都走这里, 返回错误文案或 None。"""
    if jtype in ("arena", "rank", "rank_glory"):
        fn = lambda evt: _run_battle(evt, jtype, params)     # noqa: E731
    else:
        fn = lambda evt: _run_collect(evt, params)           # noqa: E731
    return JOBS.start(jtype, params, fn)


# ================================================================ 预约启动 (到点自动开打)
class JobScheduler:
    """预约启动 —— "人不在, 到点自动开打" (2026-09-22 用户需求)。

    背景: 荣耀时刻(每晚 18-20)与全民争霸(周六/日 16-17) 的启动按钮只在窗口内
    可用, 人若不在机器前就白等一个窗口。预约把"点按钮"这件事**提前**说好, 由服务端
    后台线程在到点时自动下发任务 —— 页面关掉/浏览器最小化都不影响。

    语义 (每条都对应一个真实误解场景, 不是防御性想象):
      1) 输入 `when`: "HH:MM" (定点, 今天这点已过则顺延到明天) / "90"或"90分钟"
         (相对现在) / "30s"(秒, 调试用)。解析口径唯一在 _parse_when。
      2) 到点后 (1s tick): 若该模式**时段窗口未开** -> 默认等窗口开启再启动
         (这正是"提前约好 6 点打"的关键: 约 17:00 也会在 18:00 准时开打);
         若已有**同类型**任务在跑 -> 视为预约已被满足, 结束(不重复启动);
         若已有**其它**任务在跑 -> 等它结束再启动;
         窗口当日不会再开 / 错过预约日 -> 作废并写清原因。
      3) 预约**仅当日有效** (目标时刻所在日 23:59 为有效期上界)。宁可当场拒绝,
         也不留"几天后突然开打"这种事后无法解释的行为。
      4) 落盘 data/schedule.json (原子替换): 控制台重启后预约仍在, 到点前重启=无缝;
         重启时若目标已过但窗口还开着 -> 立即补启动 (catch-up)。
    """

    TICK_SEC = 1.0
    NOTE_LOG_SEC = 300        # 等待类提示: 同一句话最多 5 分钟记一条 (防日志刷屏)
    MAX_HORIZON_SEC = 7 * 86400

    def __init__(self, path: Path, log: BotLog, jobs: JobManager, dispatch) -> None:
        self.path = Path(path)
        self._log = log
        self._jobs = jobs
        self._dispatch = dispatch
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._last: dict[str, dict] = {}
        self._note_gate: dict[str, tuple[float, str]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    # ---------------- 线程 ----------------
    def start(self) -> None:
        """启动后台调度线程 (幂等; 任何入口调用都安全)。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="job-scheduler")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:                      # 调度线程绝不能因单次异常死掉
                try:
                    self._log.error(f"预约调度异常: {type(e).__name__}: {e}")
                except Exception:
                    pass
            time.sleep(self.TICK_SEC)

    # ---------------- 解析 ----------------
    @staticmethod
    def _human_dur(sec: float) -> str:
        sec = int(round(sec))
        if sec < 60:
            return f"{sec} 秒"
        m, s = divmod(sec, 60)
        if m < 60:
            return f"{m} 分" + (f"{s} 秒" if s else "")
        h, m = divmod(m, 60)
        return f"{h} 小时" + (f"{m} 分" if m else "")

    @staticmethod
    def _parse_when(when: str, now: _dt.datetime | None = None) -> tuple[_dt.datetime, str]:
        """预约输入 -> (目标时刻, 人类可读标签)。非法输入抛 ValueError。"""
        now = now or _dt.datetime.now()
        s = (when or "").strip().replace(" ", "")
        if not s:
            raise ValueError("请填写预约时间: 如 18:00 (定点) 或 90 (90 分钟后)")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
        if m:
            hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            if hh > 23 or mm > 59 or ss > 59:
                raise ValueError(f"时间不合法: {when}")
            target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if target <= now:                 # 今天这点已过 -> 顺延到明天
                target += _dt.timedelta(days=1)
            return target, f"{target:%m-%d %H:%M:%S}"
        m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)(秒|s|sec|分钟|分|m|min|小时|h)?", s, re.I)
        if m:
            val = float(m.group(1))
            unit = (m.group(2) or "分钟").lower()
            mult = {"秒": 1, "s": 1, "sec": 1, "分钟": 60, "分": 60,
                    "m": 60, "min": 60, "小时": 3600, "h": 3600}[unit]
            sec = val * mult
            if sec <= 0:
                raise ValueError("预约时长必须大于 0")
            target = now + _dt.timedelta(seconds=sec)
            return target, f"{target:%m-%d %H:%M:%S} (约 {JobScheduler._human_dur(sec)}后)"
        raise ValueError(f"看不懂的时间格式: {when} (可用 18:00 / 90 / 90分钟 / 30s)")

    # ---------------- 登记 / 取消 / 查询 ----------------
    def add(self, jtype: str, params: dict, when: str,
            now: _dt.datetime | None = None) -> tuple[Optional[dict], Optional[str]]:
        """登记(或覆盖)一个预约。返回 (条目, 错误文案)。"""
        from sj_bot.state_machine import window_state
        now = now or _dt.datetime.now()
        try:
            fire_at, label = self._parse_when(when, now)
        except ValueError as e:
            return None, str(e)
        if (fire_at - now).total_seconds() > self.MAX_HORIZON_SEC:
            return None, "预约时间超过 7 天, 疑似输入错误"
        expire_at = fire_at.replace(hour=23, minute=59, second=59, microsecond=0)
        # 登记即预检: 有效期内若窗口根本不会开 -> 当场拒绝, 不给用户留"假预约"
        planned = _window_open_within(jtype, fire_at, expire_at)
        if planned is None:
            ok, nxt = window_state(jtype, now)
            hint = f"最近一次 {nxt:%m-%d %H:%M} 才开放" if nxt else "该模式无可用时段"
            return None, (f"{JOB_CN.get(jtype, jtype)} 在 {expire_at:%m-%d} 当天没有可用时段"
                          f"({hint}); 预约仅当日有效, 请在开放当天再约")
        ts = time.time()
        item = {
            "type": jtype,
            "params": dict(params or {}),
            "when": when,
            "label": label,
            "fire_at": fire_at.timestamp(),
            "fire_at_str": f"{fire_at:%m-%d %H:%M:%S}",
            "expire_at": expire_at.timestamp(),
            "planned_at": planned.timestamp(),
            "created_at": _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S"),
            "state": "pending",
            "note": "",
        }
        with self._lock:
            old = self._items.get(jtype)
            self._items[jtype] = item
            self._note_gate.pop(jtype, None)
        self._save()
        if old:
            self._log.info(f"预约被覆盖: {JOB_CN.get(jtype, jtype)} "
                           f"{old.get('fire_at_str')} -> {item['fire_at_str']}")
        return item, None

    def cancel(self, jtype: Optional[str] = None) -> int:
        """取消预约 (jtype=None 取消全部)。返回取消条数。"""
        with self._lock:
            if jtype is None:
                n = len(self._items)
                self._items.clear()
            else:
                n = 1 if self._items.pop(jtype, None) is not None else 0
            self._note_gate.clear()
        if n:
            self._save()
            self._log.warn(f"已取消预约: {JOB_CN.get(jtype, '全部') if jtype else '全部'}")
        return n

    def items(self, now: _dt.datetime | None = None) -> list[dict]:
        """待执行的预约列表 (含剩余时间/预计实际启动时刻, 供页面倒计时)。"""
        from sj_bot.state_machine import window_state
        now = now or _dt.datetime.now()
        out: list[dict] = []
        with self._lock:
            snapshot = [(k, dict(v)) for k, v in self._items.items()]
        for jtype, s in snapshot:
            ok, next_open = window_state(jtype, now)
            planned = s["fire_at"]
            if not ok and next_open is not None:
                planned = max(planned, next_open.timestamp())
            d = dict(s)
            d["window_open"] = bool(ok)
            d["planned_at"] = planned
            d["planned_str"] = _dt.datetime.fromtimestamp(planned).strftime("%m-%d %H:%M")
            d["remain_sec"] = max(0, int(s["fire_at"] - now.timestamp()))
            d["wait_sec"] = max(0, int(planned - now.timestamp()))
            out.append(d)
        out.sort(key=lambda x: x["fire_at"])
        return out

    def last(self) -> dict:
        """最近一次预约触发结果 (供页面回答"到点到底打了没")。"""
        with self._lock:
            return {k: dict(v) for k, v in self._last.items()}

    # ---------------- 调度核心 ----------------
    def _tick(self, now: _dt.datetime | None = None) -> None:
        from sj_bot.state_machine import window_state
        now = now or _dt.datetime.now()
        with self._lock:
            pend = list(self._items.items())
        for jtype, s in pend:
            if now.timestamp() < s["fire_at"]:
                continue
            name = JOB_CN.get(jtype, jtype)
            if now.timestamp() > s["expire_at"]:
                self._drop(jtype, f"错过 {now:%m-%d} 当日窗口(预约仅当日有效), 已作废")
                continue
            ok, _next = window_state(jtype, now)
            if not ok:
                today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
                want = _window_open_within(jtype, now, today_end)
                if want is None:
                    self._drop(jtype, f"{name} 当日已无可用时段, 预约作废")
                else:
                    self._mark(jtype, "waiting", f"等 {name} 窗口开启")
                    self._note(jtype, f"预约已到点, 但 {name} 窗口未开"
                                     f"({now:%H:%M}); 等 {want:%H:%M} 开放后自动启动")
                continue
            st = self._jobs.status()
            if st.get("running"):
                if st.get("type") == jtype:
                    self._drop(jtype, f"已有同类型任务在运行, 预约视为已满足")
                else:
                    running_cn = JOB_CN.get(st.get("type"), st.get("type"))
                    self._mark(jtype, "waiting", f"等前一个任务({running_cn})结束")
                    self._note(jtype, f"预约已到点, 但 {running_cn} 仍在运行; "
                                      f"等它结束后自动启动 {name}")
                continue
            self._fire(jtype, s)

    def _fire(self, jtype: str, s: dict) -> None:
        with self._lock:
            self._items.pop(jtype, None)
            self._note_gate.pop(jtype, None)
        self._jobs.clear_if_done()
        params = dict(s.get("params") or {})
        err = self._dispatch(jtype, params)
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        name = JOB_CN.get(jtype, jtype)
        if err:
            self._log.error(f"预约启动失败({name} {params}): {err} —— 预约已结束, 不会重试")
            self._record_last(jtype, False, f"{stamp} 启动失败: {err}")
        else:
            self._log.ok(f"预约触发: {name} {params} 已自动启动 "
                         f"(预约时间 {s.get('label')}) —— 无需人工点按钮")
            self._record_last(jtype, True, f"{stamp} 已自动启动 {params}")
        self._save()

    def _drop(self, jtype: str, reason: str) -> None:
        with self._lock:
            self._items.pop(jtype, None)
            self._note_gate.pop(jtype, None)
        self._log.warn(f"预约作废({JOB_CN.get(jtype, jtype)}): {reason}")
        self._record_last(jtype, False, reason)
        self._save()

    def _mark(self, jtype: str, state: str, note: str) -> None:
        with self._lock:
            s = self._items.get(jtype)
            if s:
                s["state"] = state
                s["note"] = note

    def _note(self, jtype: str, msg: str) -> None:
        """等待类日志: 同一句话在 NOTE_LOG_SEC 内只记一条 (轮询 1s 否则会刷屏)。"""
        with self._lock:
            last_ts, last_msg = self._note_gate.get(jtype, (0.0, ""))
            t = time.time()
            if msg == last_msg and (t - last_ts) < self.NOTE_LOG_SEC:
                return
            self._note_gate[jtype] = (t, msg)
        self._log.info(msg)

    def _record_last(self, jtype: str, ok: bool, msg: str) -> None:
        with self._lock:
            self._last[jtype] = {
                "ts": _dt.datetime.now().strftime("%m-%d %H:%M:%S"),
                "ok": bool(ok), "msg": msg,
            }

    # ---------------- 落盘 ----------------
    def _save(self) -> None:
        with self._lock:
            data = json.dumps({"items": self._items, "last": self._last},
                              ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self.path)          # 原子替换: 崩溃/断电不留半截 JSON
        except Exception as e:
            try:
                self._log.warn(f"预约落盘失败: {type(e).__name__}: {e}")
            except Exception:
                pass

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:
            try:
                self._log.warn(f"预约文件读取失败(已忽略): {type(e).__name__}: {e}")
            except Exception:
                pass
            return
        if not isinstance(raw, dict):
            return
        items = raw.get("items")
        keep: dict[str, dict] = {}
        if isinstance(items, dict):
            now = time.time()
            for jtype, s in items.items():
                if not isinstance(s, dict) or "fire_at" not in s:
                    continue
                if now > float(s.get("expire_at", 0)):
                    continue                       # 有效期已过: 恢复也无意义
                s["state"] = "pending"
                s["note"] = ""
                keep[jtype] = s
        with self._lock:
            self._items = keep
            self._last = raw.get("last") if isinstance(raw.get("last"), dict) else {}
        if keep:
            try:
                self._log.info("已恢复预约: " + ", ".join(
                    f"{JOB_CN.get(k, k)} {v.get('fire_at_str')} ({v.get('when')})"
                    for k, v in keep.items()))
            except Exception:
                pass


def _window_open_within(jtype: str, start: _dt.datetime,
                        end: _dt.datetime) -> Optional[_dt.datetime]:
    """[start, end] 内该模式**最早**的可用时刻; 完全没有可用时段返回 None。

    先问 window_state 给出的**精确**开放边界 (所有窗口都在整点: 16/17/18/20 点),
    只有在边界异常时才退化为 15 分钟步进兜底。
    历史教训: 曾纯靠 15 分钟步进采样, 结果把真实边界量化到采样格点上 ——
    16:51 约的荣耀被日志报成"等 18:06 开放"(应为 18:00), 属于会误导人的错时刻。
    """
    from sj_bot.state_machine import window_state
    ok, nxt = window_state(jtype, start)
    if ok:
        return start
    if nxt is not None:
        if nxt > end:
            return None
        if window_state(jtype, nxt)[0]:
            return nxt
        start = nxt                       # 边界自相矛盾: 退化为步进兜底
    step = start
    while step <= end:
        if window_state(jtype, step)[0]:
            return step
        step += _dt.timedelta(minutes=15)
    return None


SCHED = JobScheduler(cfg.data_dir / "schedule.json", LOG, JOBS, _dispatch_job)


# ================================================================ 页面
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


# ================================================================ API: 状态
def _last_job_summary() -> Optional[dict]:
    """最近一次**已结束**任务的结论 —— 让用户"一眼看到上次为什么停" (2026-09-22 需求)。

    优先取内存里保留的任务槽位 (刚结束那一次最新); 若当前正在跑 (槽位被在跑的任务占用)
    或控制台重启过 (槽位已清), 则回落到落盘历史 data/job_history.jsonl ——
    这样"人不在时停了, 回来一眼就能看到原因"在重启后也成立。
    """
    def pack(src: dict, ts_key: str) -> dict:
        jtype = src.get("type")
        return {
            "type": jtype,
            "type_cn": JOB_CN.get(jtype or "", jtype),
            "state": src.get("state"),
            "ts": src.get(ts_key),
            "error": src.get("error"),
            "result": enrich_result(src.get("result")),
        }

    cur = JOBS.status()
    finished_now = (not cur.get("running")) and (
        isinstance(cur.get("result"), dict) or cur.get("state") in ("done", "error"))
    if finished_now:
        return pack(cur, "started_at")
    if JOBS._history is not None:
        items = JOBS._history.recent(1)
        if items:
            return pack(items[0], "ts")
    return None


@app.route("/api/status")
def api_status():
    import datetime as _dt
    from sj_bot.state_machine import in_window, next_open_start, WEEKLY_POINTS_CAP
    from sj_bot.db import OpponentDB

    db = OpponentDB(cfg.db_path)
    st = db.stats()
    pts = db.weekly_win_points()
    db.close()
    now = _dt.datetime.now()
    from sj_bot.state_machine import in_glory_window, GLORY_START_HOUR, GLORY_END_HOUR
    try:
        from sj_bot.llm_client import get_budget as _llm_budget
        lb = _llm_budget()
    except Exception:
        lb = {}
    return jsonify({
        "serial": SERIAL,
        "now": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "arena_open": in_window(now),
        "next_open": next_open_start(now).strftime("%m-%d %H:%M") if not in_window(now) else "",
        "glory_open": in_glory_window(now),
        "glory_window": f"{GLORY_START_HOUR:02d}:00-{GLORY_END_HOUR:02d}:00",
        "db": st,
        "points": pts,
        "points_cap": WEEKLY_POINTS_CAP,
        "llm_budget": lb,   # 今日模型付费调用 {calls, cap} (省钱约定: 本地方法优先)
        "schedule": SCHED.items(),        # 待执行的预约启动 (到点自动开打)
        "schedule_last": SCHED.last(),    # 最近一次预约触发结果
        "last_job": _last_job_summary(),  # 最近一次已结束任务 + 人话版终止原因
    })


# ================================================================ API: 任务
@app.route("/api/jobs/start", methods=["POST"])
def api_jobs_start():
    JOBS.clear_if_done()
    body = request.get_json(force=True, silent=True) or {}
    jtype = body.get("type", "arena")
    if jtype not in ("arena", "rank", "rank_glory", "collect"):
        return jsonify({"error": f"未知任务类型 {jtype}"}), 400
    p, err = _norm_params(jtype, body.get("params") or {})
    if err:
        return jsonify({"error": err}), 400
    werr = _window_error(jtype)          # 时段互斥: 窗口外/被荣耀占用 -> 拒启
    if werr:
        return jsonify({"error": werr}), 400
    err = _dispatch_job(jtype, p)
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"ok": True, "type": jtype, "params": p})


# ================================================================ API: 预约启动
@app.route("/api/jobs/schedule", methods=["GET"])
def api_jobs_schedule_list():
    return jsonify({"items": SCHED.items(), "last": SCHED.last()})


@app.route("/api/jobs/schedule", methods=["POST"])
def api_jobs_schedule_add():
    """登记预约: {type, when:"18:00"|"90"|"30s", params:{...}} —— 到点自动下发。"""
    SCHED.start()                        # 幂等: 任何入口都用过就该有调度线程在跑
    body = request.get_json(force=True, silent=True) or {}
    jtype = body.get("type", "")
    if jtype not in ("arena", "rank", "rank_glory", "collect"):
        return jsonify({"error": f"未知任务类型 {jtype}"}), 400
    p, err = _norm_params(jtype, body.get("params") or {})
    if err:
        return jsonify({"error": err}), 400
    item, err = SCHED.add(jtype, p, body.get("when", ""))
    if err:
        return jsonify({"error": err}), 400
    LOG.info(f"已预约: {JOB_CN.get(jtype, jtype)} 将于 {item['fire_at_str']} 自动启动"
             f"(输入 {item['when']}); 到点无需任何人工操作")
    return jsonify({"ok": True, "schedule": item, "items": SCHED.items()})


@app.route("/api/jobs/schedule/cancel", methods=["POST"])
def api_jobs_schedule_cancel():
    body = request.get_json(force=True, silent=True) or {}
    n = SCHED.cancel(body.get("type") or None)
    if not n:
        return jsonify({"error": "没有待执行的预约"}), 400
    return jsonify({"ok": True, "cancelled": n, "items": SCHED.items()})


@app.route("/api/jobs/stop", methods=["POST"])
def api_jobs_stop():
    if not JOBS.running():
        return jsonify({"error": "当前没有运行中的任务"}), 400
    JOBS.stop()
    return jsonify({"ok": True})


@app.route("/api/jobs/status")
def api_jobs_status():
    j = JOBS.status()
    if "result" in j:
        j["result"] = enrich_result(j["result"])
    return jsonify(j)


@app.route("/api/jobs/history")
def api_jobs_history():
    items = JOBS._history.recent(12) if JOBS._history else []
    out = []
    for it in items:
        it = dict(it)
        if "result" in it:
            it["result"] = enrich_result(it["result"])
        out.append(it)
    return jsonify({"items": out})


# ---- 兼容旧端点: /api/collect/* ----
@app.route("/api/collect/start", methods=["POST"])
def api_collect_start_legacy():
    body = request.get_json(force=True, silent=True) or {}
    out = api_passthrough("collect", body)
    return jsonify(out), (200 if out.get("ok") else 409)


def api_passthrough(jtype: str, body: dict):
    JOBS.clear_if_done()
    pages = min(int(body.get("pages", 40)), 400)
    fn = _run_collect
    p = {"pages": pages}
    err = JOBS.start(jtype, p, lambda evt: fn(evt, p))
    return {"ok": True, "pages": pages} if not err else {"error": err}


@app.route("/api/collect/status")
def api_collect_status_legacy():
    return jsonify({"running": JOBS.status()["running"]})


@app.route("/api/collect/stop", methods=["POST"])
def api_collect_stop_legacy():
    if not JOBS.running():
        return jsonify({"error": "没有运行中的任务"}), 400
    JOBS.stop()
    return jsonify({"ok": True})


# ================================================================ API: 画面(调试保留)
@app.route("/api/shot")
def api_shot():
    data = _shot()
    if data is None:
        return jsonify({"error": "截图失败: 模拟器离线?"}), 503
    return send_file(io.BytesIO(data), mimetype="image/png")


@app.route("/api/tap", methods=["POST"])
def api_tap():
    body = request.get_json(force=True, silent=True) or {}
    x, y = int(body.get("x", -1)), int(body.get("y", -1))
    if x < 0 or y < 0:
        return jsonify({"error": "需要 x,y"}), 400
    _adb(["shell", "input", "tap", str(x), str(y)])
    LOG.info(f"手动点击 ({x}, {y})")
    return jsonify({"ok": True, "x": x, "y": y})


@app.route("/api/swipe", methods=["POST"])
def api_swipe():
    body = request.get_json(force=True, silent=True) or {}
    x1, y1 = int(body.get("x1", 0)), int(body.get("y1", 0))
    x2, y2 = int(body.get("x2", 0)), int(body.get("y2", 0))
    _adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "200"])
    LOG.info(f"手动滑动 ({x1},{y1}) -> ({x2},{y2})")
    return jsonify({"ok": True})


# ================================================================ API: OCR(调试/标定用)
_ocr = None
_ocr_lock = threading.Lock()


def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


@app.route("/api/ocr", methods=["POST"])
def api_ocr():
    data = _shot()
    if data is None:
        return jsonify({"error": "截图失败"}), 503
    import cv2
    import numpy as np
    nparr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    with _ocr_lock:
        res, _ = _get_ocr()(img)
    items = []
    if res:
        for box, text, score in res:
            if not (isinstance(text, str) and text.strip()):
                continue
            xs = [p[0] for p in box if isinstance(p, (list, tuple)) and len(p)
                  and isinstance(p[0], (int, float))]
            ys = [p[1] for p in box if isinstance(p, (list, tuple)) and len(p) >= 2
                  and isinstance(p[1], (int, float))]
            if not xs or not ys:
                continue
            items.append({"x": int(min(xs)), "y": int(min(ys)), "text": text.strip(),
                          "score": round(float(score), 2) if isinstance(score, (int, float)) else None})
    items.sort(key=lambda d: (d["y"], d["x"]))
    return jsonify({"count": len(items), "items": items})


# ================================================================ API: 档案库
@app.route("/api/db/stats")
def api_db_stats():
    db = OpponentDB(cfg.db_path)
    st = db.stats()
    db.close()
    return jsonify(st)


@app.route("/api/db/names")
def api_db_names():
    verdict = request.args.get("verdict", "no")
    limit = int(request.args.get("limit", 200))
    db = OpponentDB(cfg.db_path)
    rows = db.list_by_verdict(verdict)  # type: ignore[arg-type]
    db.close()
    names = [r["name"] for r in rows]
    return jsonify({"total": len(names), "names": names[:limit]})


# ================================================================ API: 日志
# ================================================================ API: 开箱奖励 (2026-09-29)
# 数据源是引擎在"开箱奖励"弹窗那一刻落下的 data/rewards.jsonl (由
# Battler._save_reward_frame 写入), 每行一条 {ts, mode, round, verdict, file}。
# 用 append-only jsonl 而不是 JSON 数组: 挂机途中进程随时可能被回收, 现有几行
# 就保住几行, 不像整体重写那样一崩全丢。
REWARDS_JSONL = cfg.data_dir / "rewards.jsonl"

# 「宝箱奖励」弹窗的裁剪 ROI (1600x900 全图绝对坐标) —— 2026-09-29 由首两张真实帧标定。
# 弹窗位置在结算链里是固定的 (和"比赛结束(801,697)"同一套版式), 所以能写死;
# 若哪天界面对不上, /api/rewards/img 会自动退回整帧而不是给一张错图。
REWARD_CROPS = {
    "icon":  (690, 298, 882, 470),      # 弹窗中央那枚大图标 (缩略图用, 看得清是啥)
    "popup": (520, 190, 1090, 610),     # 整个「宝箱奖励」弹窗 (含标题与 xN 角标)
}


@app.route("/api/rewards")
def api_rewards():
    """最近的开箱奖励明细 (倒序)。file 字段可直接喂给 /api/rewards/img。"""
    limit = max(1, min(int(request.args.get("limit", 40)), 300))
    if not REWARDS_JSONL.exists():
        return jsonify({"total": 0, "items": []})
    rows: list[dict] = []
    try:
        with REWARDS_JSONL.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue          # 半行(崩在写入中间)直接跳过, 不影响其它记录
    except OSError as e:
        return jsonify({"error": f"奖励记录读取失败: {e!r}"}), 500
    return jsonify({"total": len(rows), "items": list(reversed(rows[-limit:]))})


@app.route("/api/rewards/img")
def api_rewards_img():
    """按 rewards.jsonl 的 file 字段回图。限制在 captures/rewards/ 内, 只给 png。

    可选 `crop=icon|popup` (2026-09-29): 存盘留的是**整帧** (要当证据, 含战斗背景与
    三个宝箱); 但整帧缩到侧栏 130px 宽根本看不清奖励是啥。所以裁剪放在**请求时**做,
    存证与看清两件事分开 —— 不改动磁盘上的原图。
    """
    rel = (request.args.get("f") or "").strip()
    root = (cfg.capture_dir / "rewards").resolve()
    p = (cfg.capture_dir / rel).resolve()
    try:
        inside = p.is_relative_to(root)     # 防 ../ 穿越
    except ValueError:
        inside = False
    if not inside or p.suffix.lower() != ".png" or not p.exists():
        return jsonify({"error": "not found"}), 404

    roi = REWARD_CROPS.get((request.args.get("crop") or "").strip().lower())
    if not roi:
        return send_file(str(p), mimetype="image/png")
    import cv2
    import numpy as np
    img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)   # 中文路径必须走 fromfile
    if img is None:
        return send_file(str(p), mimetype="image/png")                   # 解不开就退整帧, 不报错
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (max(0, roi[0]), max(0, roi[1]), min(roi[2], w), min(roi[3], h))
    if x1 <= x0 or y1 <= y0:
        return send_file(str(p), mimetype="image/png")                   # 分辨率变了就退整帧
    ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
    if not ok:
        return send_file(str(p), mimetype="image/png")
    return send_file(io.BytesIO(buf.tobytes()), mimetype="image/png")


@app.route("/api/logs")
def api_logs():
    after = int(request.args.get("after", 0))
    cur, lines = LOG.lines(after_seq=after)
    return jsonify({"seq": cur, "lines": lines})


# ================================================================ 浏览器自动打开 (仅 start_console.bat 双击场景)
def _maybe_autoopen_browser(port: int) -> None:
    """由 .bat 双击启动时 (环境变量 SJ_OPEN_BROWSER=1), 服务就绪后自动打开浏览器.
    轮询自身端口直至可访问再 webbrowser.open, 避免"浏览器先于服务打开"的空页面.
    其它启动方式 (命令行/后台脚本) 不设该变量, 此函数直接返回, 不影响任何既有流程."""
    import os
    if os.environ.get("SJ_OPEN_BROWSER") != "1":
        return
    import urllib.request
    import webbrowser
    max_sec = int(os.environ.get("SJ_AUTOOPEN_MAX_SEC", "60"))

    def _wait_and_open() -> None:
        url = f"http://127.0.0.1:{port}/api/status"
        t0 = time.time()
        while time.time() - t0 < max_sec:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        webbrowser.open(f"http://127.0.0.1:{port}")
                        return
            except Exception:
                pass
            time.sleep(1)

    threading.Thread(target=_wait_and_open, daemon=True).start()


if __name__ == "__main__":
    # 先把今天的日志文件尾部读回内存环形缓冲 (09-29): 重启后刷新页面还能看到
    # 重启前的日志, 而不是从空白开始 —— 这是"复盘可用"的最低要求。
    _backfilled = LOG.load_recent(200)
    LOG.info(f"控制台启动: 设备 {SERIAL}, 页面 http://127.0.0.1:5050"
             + (f"  (已从今日日志回填 {_backfilled} 条)" if _backfilled else ""))
    LOG.info(f"日志落盘: {LOG.log_file}")
    SCHED.start()          # 预约启动调度线程 (落盘的预约在重启后继续生效)
    _maybe_autoopen_browser(5050)
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False, threaded=True)
