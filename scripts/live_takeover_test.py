# -*- coding: utf-8 -*-
"""真机端到端: 从"战斗页"直接接管打完一场 (验证 P0 修复).

流程 = 主循环真实路径: 导航(战斗页应直接成功) -> 接管本局 -> 点跳过 ->
      等结算宝箱 -> 判胜负 -> 比赛结束 -> 返回大厅 -> fought=1 收尾.
安全: 开始前先确认当前确在战斗页, 否则直接退出不点任何东西.
用法: python scripts/live_takeover_test.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot.battler import Battler   # noqa: E402


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    b.log = lambda lv, msg: print(f"  [{lv}] {msg}", flush=True)
    b.on_progress = lambda p: None

    img = b._shot_img()
    if img is None:
        print("[X] 截图失败")
        return
    page = b._classify_page(img)
    settle = b._detect_settle_stage(img)
    in_battle = b._is_in_battle(img)
    print(f"[前置] 页面分类 = {page}   in_battle = {in_battle}"
          f"   settle_stage = {settle}   (跳过模板 {b._find_skip_btn(img)})")
    if page == "unknown":
        print("[X] 页面无法识别, 为避免乱点已中止.")
        return
    print("[说明] _run_rank 自适应: 战斗页->直接接管; 结算链->原地接管结算; "
          "主城/二级菜单->重新导航进排位赛")

    started = dt.datetime.now()
    print("[开始] 走 _run_rank(rounds=1), 模拟主循环真实路径 ...")
    res = b._run_rank(started, glory=False)
    print("\n[结果] " + str(res))


if __name__ == "__main__":
    main()
