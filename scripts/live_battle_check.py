# -*- coding: utf-8 -*-
"""真机只读校验: 战斗页能否被 _enter_pvp_hub 正确识别为"已在流程内".

安全: 全程拦截 _tap, 不会真的点击游戏 (只截图 + 判定 + 干跑导航).
用法: python scripts/live_battle_check.py
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot.battler import Battler   # noqa: E402


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    b.stop_evt = threading.Event()

    img = b._shot_img()
    if img is None:
        print("[X] 截图失败 (设备未连接?)")
        return
    out = Path(b.cfg.capture_dir) / "live_battle_check.png"
    import cv2
    cv2.imencode(".png", img)[1].tofile(str(out))
    print(f"[img] {img.shape} -> {out}")

    hit = b._find_skip_btn(img)
    print(f"[1] _find_skip_btn  = {hit}")
    page = b._classify_page(img)
    print(f"[2] _classify_page  = {page}")

    # 干跑导航: 拦截点击 / 跳过 sleep, 只观察分支走向
    taps = []
    anomalies = []
    b._tap = lambda x, y: (taps.append((x, y)), print(f"    (拦截点击 {x},{y})"))
    b._sleep_stop = lambda s: None
    b._save_anomaly = lambda tag: (anomalies.append(tag), print(f"    (存异常图 {tag})"))
    b.log = lambda lv, msg: print(f"    [{lv}] {msg}")

    ok = b._enter_pvp_hub("rank")
    print(f"[3] _enter_pvp_hub('rank') -> {ok}   落点 _nav_page={b._nav_page}")
    print(f"    拦截到的点击 = {taps}")
    print(f"    异常图 = {anomalies}")

    verdict = "PASS" if (hit and page == "battle" and ok and not taps
                         and b._nav_page == "battle" and not anomalies) else "CHECK"
    print(f"\n==> {verdict}: 战斗页 -> 导航直接成功/零点击/无异常图" if verdict == "PASS"
          else f"\n==> {verdict}: 当前页面可能不是战斗页, 请看上面判定结果")


if __name__ == "__main__":
    main()
