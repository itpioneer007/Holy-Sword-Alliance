r"""每日开盘前的「冷启动链」—— 无人值守的第一步 (2026-09-29)。

解决的痛点 (用户原话)
    "我有时候会忘记在晚上 6 点预约启动怎么办, 我还要启动 MuMu 模拟器,
     点击游戏进行登录, 才能让你的脚本开始操作, 有点麻烦。"

把"人到点要手动做的四件事"串成一条自动链:

    1. 启动 MuMu 实例        —— 幂等: 已经在跑就跳过
    2. 等 adb 就绪           —— 轮询 adb connect + devices, 不靠固定 sleep
    3. 把游戏拉到前台        —— 复用 Battler._ensure_game_foreground / _launch_game
    4. 完成登录              —— 登录页 -> 点「开始」-> 活动开场页 -> 主城
    5. 登记当日预约          —— rank_glory@18:00; 周末另加 arena@16:00

    最后把全过程写进 data/logs/prep-YYYY-MM-DD.log, 事后可复盘。

为什么登录链要自己写而不直接调 _recover_from_session
    那个方法是**战斗期**的会话自愈: 它把"恢复成功"记进五族自愈计数、并按排位
    主页(hub)作为落点。准备阶段不该消耗战斗期的自愈预算, 也只需要到主城
    (后面的导航是任务自己的事)。所以这里只复用 Battler 的**判据与动作原语**
    (_is_login_screen / _is_in_main_city / _tap / _press_back), 不新造判据。

安全闸门 (为什么敢无人值守地乱点)
    · 任何会点屏幕的操作前, 先查 /api/jobs/status。**有任务在跑就立刻退出**,
      绝不干预正在进行的对局。
    · **只在登录页才点**「开始」; back 键只在"既不是登录页也不是主城"时才按,
      且有次数上限 —— 登录页上按 back 会把游戏退到桌面。
    · --dry 只探测不动作, 可任何时候安全试跑。

用法
    <venv python> scripts\daily_prep.py                # 全自动
    <venv python> scripts\daily_prep.py --dry           # 只探测, 不启动不点击不预约
    <venv python> scripts\daily_prep.py --skip-mumu     # 不碰模拟器 (假定已在跑)
    <venv python> scripts\daily_prep.py --no-schedule   # 只做准备, 不登记预约
    <venv python> scripts\daily_prep.py --status        # 只打印当前状态
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2                                   # noqa: E402
import numpy as np                           # noqa: E402

from sj_bot import rank_layout as rk         # noqa: E402
from sj_bot.battler import Battler           # noqa: E402
from sj_bot.config import Config             # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "data" / "logs"

MUMU_MANAGER = Path(r"D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe")
MUMU_VM = "0"                    # 实例序号 (vm_config: vms/MuMuPlayer-15.0-0)
API = "http://127.0.0.1:5050"

# 登录页 -> 主城 的等待与取样节奏。实测: 点「开始」后 20~40s 落到活动开场页,
# 该页不会自己消失, 需要一次 back; 给足 150s 覆盖慢启动。
SAMPLE_EVERY = 4.0
LOGIN_BUDGET_SEC = 150.0
SPLASH_RECHECK_SEC = 12.0        # 超过这么久还没进主城, 就考虑按一次 back
MAX_BACK = 2


# ---------------------------------------------------------------- 日志
class PrepLog:
    """同时写 stdout 与 data/logs/prep-YYYY-MM-DD.log。"""

    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"prep-{dt.date.today():%Y-%m-%d}.log"
        self.fh = self.path.open("a", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        line = f"[prep {dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


log: "PrepLog"


# ---------------------------------------------------------------- 工具
def _http_json(path: str, body: dict | None = None, timeout: float = 10.0):
    """访问本地控制台。**必须绕代理** —— 沙箱环境有 HTTP_PROXY, 直连会拿到 502 假死。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = API + path
    if body is None:
        return json.load(op.open(url, timeout=timeout))
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(op.open(req, timeout=timeout))


def port_up(port: int = 5050, timeout: float = 1.5) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def save_png(img: "np.ndarray", path: Path) -> None:
    """⚠️ 项目根路径含中文: cv2.imwrite 会**静默失败**(返 False 不抛异常),
    必须走 imencode + tofile —— 与 battler._save_anomaly 同一写法。"""
    cv2.imencode(".png", img)[1].tofile(str(path))


# ---------------------------------------------------------------- 第 1 步: 模拟器
def mumu_info() -> dict:
    if not MUMU_MANAGER.exists():
        return {"error": f"找不到 MuMuManager: {MUMU_MANAGER}"}
    try:
        r = subprocess.run([str(MUMU_MANAGER), "info", "-v", MUMU_VM],
                           capture_output=True, timeout=40)
        return json.loads((r.stdout or b"{}").decode("utf-8", "replace"))
    except Exception as e:
        return {"error": repr(e)}


def ensure_mumu(dry: bool) -> str:
    info = mumu_info()
    if "error" in info:
        log(f"模拟器探测失败: {info['error']}")
        return "unknown"
    if info.get("is_android_started"):
        log("模拟器已在运行, 跳过启动")
        return "running"
    if dry:
        log("[DRY] 模拟器未运行 -> 本应执行 MuMuManager control -v 0 launch")
        return "dry"
    log("模拟器未运行 -> 启动实例 0 ...")
    try:
        r = subprocess.run([str(MUMU_MANAGER), "control", "-v", MUMU_VM, "launch"],
                           capture_output=True, timeout=120)
        log(f"  launch 返回: {(r.stdout or b'').decode('utf-8','replace').strip()[:200]}")
    except Exception as e:
        log(f"  启动模拟器异常: {e!r}")
        return "failed"
    return "launched"


def wait_adb(serial: str, adb: str, timeout: float = 180.0) -> bool:
    """轮询 adb connect + devices 直到设备在线。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            subprocess.run([adb, "connect", serial], capture_output=True, timeout=15)
            out = subprocess.run([adb, "devices"], capture_output=True, timeout=15).stdout
            if serial.encode() in (out or b""):
                log(f"adb 就绪 ({time.time()-t0:.0f}s): {serial}")
                return True
        except Exception:
            pass
        time.sleep(5)
    log(f"adb 在 {timeout:.0f}s 内未就绪")
    return False


# ---------------------------------------------------------------- 第 3~4 步: 游戏与登录
def login_step(b: Battler, dry: bool) -> str:
    """把游戏带到主城。返回 'main_city' / 'dry' / 'failed:<原因>'。"""
    img = b._shot_img()
    if img is None:
        return "failed:截图失败(adb?)"

    if b._is_in_main_city(img):
        log("已在主城, 无需登录")
        return "main_city"

    if not b._is_login_screen(img):
        log("既不在登录页也不在主城 -> 交给任务自己的导航处理, 准备阶段不干预")
        return "main_city"      # 视为"无需登录", 不阻断后续

    if dry:
        log("[DRY] 检测到登录页 -> 本应点击「开始」并处理活动开场页")
        return "dry"

    log(f"检测到登录页 -> 点击「开始」@ {rk.LOGIN_START_COORD}")
    b._tap(*rk.LOGIN_START_COORD)

    t0 = time.time()
    last_tap = t0
    backs = 0
    saw_login_page_time = 0.0
    shot_n = 0
    while time.time() - t0 < LOGIN_BUDGET_SEC:
        time.sleep(SAMPLE_EVERY)
        shot_n += 1
        img = b._shot_img()
        if img is None:
            continue

        if b._is_in_main_city(img):
            log(f"登录完成 -> 主城 (耗时 {time.time()-t0:.0f}s, 取样 {shot_n} 次)")
            return "main_city"

        if b._is_login_screen(img):
            # 还停在登录页: 可能是上一次点击没生效, 20s 后补一次 (不按 back!)
            saw_login_page_time = time.time()
            if time.time() - last_tap > 20:
                log("  仍在登录页 -> 补一次「开始」")
                b._tap(*rk.LOGIN_START_COORD)
                last_tap = time.time()
            continue

        # 既不是登录页也不是主城 —— 典型的"活动开场页"(契约战令, 全屏美术,
        # 底部"点击任意位置进入游戏", 实测不会自动消失), 按一次 back 即可回主城。
        page = b._classify_page(img)
        if backs < MAX_BACK and time.time() - t0 > SPLASH_RECHECK_SEC:
            backs += 1
            save_png(img, LOG_DIR / f"prep_splash_before_back{backs}.png")
            log(f"  识别为 {page} (非登录页/非主城) -> 按第 {backs} 次 back 尝试回主城")
            b._press_back()
            last_tap = time.time()

    return f"failed:登录超时({LOGIN_BUDGET_SEC:.0f}s, 已在登录页={saw_login_page_time>0})"


# ---------------------------------------------------------------- 第 5 步: 预约登记
def pick_targets(now: dt.datetime, force: str | None) -> list[tuple[str, str]]:
    """今天该约什么、约到几点。

    荣耀时刻 18:00-20:00 每天开; 全民争霸 16:00-17:00 只在周六/日开。
    只登记"尚未开始"的窗口 (已过点的登记会被服务端以"当天没有可用时段"拒绝)。
    """
    if force == "none":
        return []
    if force in ("rank_glory", "arena"):
        when = "18:00" if force == "rank_glory" else "16:00"
        return [(force, when)]

    out: list[tuple[str, str]] = []
    if now.weekday() >= 5 and now.hour < 16:
        out.append(("arena", "16:00"))
    if now.hour < 18:
        out.append(("rank_glory", "18:00"))
    return out


def register(jtype: str, when: str, dry: bool) -> bool:
    if dry:
        log(f"[DRY] 本应预约 {jtype} @ {when}")
        return True
    try:
        d = _http_json("/api/jobs/schedule", {"type": jtype, "when": when,
                                              "params": {"rounds": 0}}, timeout=15)
    except Exception as e:
        log(f"预约 {jtype}@{when} 失败: {e!r}")
        return False
    item = d.get("schedule") or {}
    log(f"已预约 {jtype} -> {item.get('fire_at_str')} ({item.get('label','')})")
    return True


# ---------------------------------------------------------------- 主流程
def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只探测, 不启动/不点击/不预约")
    ap.add_argument("--skip-mumu", action="store_true", help="不碰模拟器")
    ap.add_argument("--no-schedule", action="store_true", help="不做预约登记")
    ap.add_argument("--target", default="auto", choices=["auto", "rank_glory", "arena", "none"])
    ap.add_argument("--status", action="store_true", help="只打印状态后退出")
    a = ap.parse_args()

    log = PrepLog()
    log("=" * 60)
    log(f"冷启动链开始  dry={a.dry} skip_mumu={a.skip_mumu} target={a.target}")

    cfg = Config()
    serial = cfg.serial or "127.0.0.1:16384"
    adb = cfg.adb_exe or "adb"
    rc = 0

    if a.status:
        log(f"配置: adb={adb} serial={serial} pkg={cfg.game_pkg}")
        log(f"模拟器: {json.dumps(mumu_info(), ensure_ascii=False)[:300]}")
        log(f"控制台 5050 在用: {port_up()}  MuMuManager 存在: {MUMU_MANAGER.exists()}")
        log.close()
        return 0

    # ---- 0. 安全闸门: 有任务在跑就绝不干预 ----
    if port_up():
        try:
            j = _http_json("/api/jobs/status", timeout=8)
            if j.get("running") or j.get("state") == "running":
                log("控制台报告有任务正在运行 -> 本次不做任何操作 (避免打断对局)")
                log.close()
                return 0
        except Exception as e:
            log(f"任务状态查询失败 (按无任务继续): {e!r}")
    else:
        log("WARN 控制台 5050 未监听 -> 预约登记会失败; 看门狗任务应在 10 分钟内拉起它")

    # ---- 0.5 先算"今天还有没有要登记的窗口" ----
    # 放在启动模拟器**之前**是有意的: 本任务每天触发两次 (15:40 / 17:40), 工作日
    # 的 15:40 那次其实无事可做 —— 若照样把 MuMu 和游戏拉起来, 就会白跑两个半小时
    # (而 MuMu 在内存压力下约每 7 分钟回收一次游戏进程, 空转越久越容易死)。
    targets = pick_targets(dt.datetime.now(), None if a.target == "auto" else a.target)
    if not targets and not a.no_schedule:
        log(f"按当前星期/时刻 ({dt.datetime.now():%m-%d %H:%M}) 无待登记窗口 -> 无事可做, 直接结束")
        log.close()
        return 0
    log(f"本次目标: {targets or '(仅做准备, 不登记)'}")

    # ---- 1. 模拟器 ----
    if not a.skip_mumu:
        st = ensure_mumu(a.dry)
        if st == "failed" and not a.dry:
            log("模拟器启动失败 -> 中止")
            log.close()
            return 2
    else:
        log("按 --skip-mumu 跳过模拟器启动")

    # ---- 2. adb ----
    if a.dry:
        log("[DRY] 跳过 adb 等待")
    elif not wait_adb(serial, adb):
        log("adb 未就绪 -> 中止")
        log.close()
        return 2

    # ---- 3~4. 游戏 + 登录 ----
    login_result = "skipped"
    if not a.dry:
        b = Battler(mode="rank_glory", rounds=0, log=lambda lv, m: log(f"  [battler:{lv}] {m}"))
        health = b._check_game_health()
        log(f"游戏存活状态: {health}")
        if health in ("gone", "background"):
            if a.dry:
                log("[DRY] 本应拉起游戏")
            else:
                log("拉起游戏到前台 ...")
                b._launch_game()
                time.sleep(10)
        login_result = login_step(b, a.dry)
        log(f"登录结果: {login_result}")
        if str(login_result).startswith("failed"):
            rc = 3
    else:
        b = Battler(mode="rank_glory", rounds=0, log=lambda lv, m: None)
        login_result = login_step(b, True)
        log(f"登录结果(DRY): {login_result}")

    # ---- 5. 预约 (targets 已在第 0.5 步算好) ----
    if a.no_schedule:
        log("按 --no-schedule 跳过预约登记")
    else:
        for jtype, when in targets:
            if not register(jtype, when, a.dry):
                rc = 4

    # ---- 收尾: 打印当前预约 ----
    if port_up():
        try:
            s = _http_json("/api/jobs/schedule", timeout=8)
            log(f"当前预约: {json.dumps(s.get('items'), ensure_ascii=False)[:400]}")
        except Exception:
            pass

    log(f"冷启动链结束 rc={rc}")
    log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
