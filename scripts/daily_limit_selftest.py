# -*- coding: utf-8 -*-
"""每日 30 次上限弹窗检测自测 (2026-09-09):
1) 上限文案命中 daily_limit; 2) 冷却文案不误触 daily_limit;
3) 上限文案不误触冷却 (否则会进入 15 分钟无谓等待); 4) 普通主页文案两者都不触发."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from sj_bot.battler import Battler
from sj_bot import rank_layout as rk

DAILY_TEXT = "你今日已累计挑战30次，请明日再来吧！其中18-20点不受场次限制哦"
COOLDOWN_TEXT = "你今日已挑战10次，请休息15分钟，稍后再来吧"
HUB_TEXT = "下次比赛时间 01:43 今日成绩:胜14 败16 单人匹配 组队匹配 受邀列表"

def make_battler(screen_text: str):
    b = object.__new__(Battler)
    img = np.zeros((900, 1600, 3), np.uint8)
    b.last_img = img
    b._shot_img = lambda: img
    b.log = lambda *a, **k: None
    b._save_stage = lambda tag: None

    def fake_find(im, hints, roi=None, scale=1.0):
        for h in hints:
            if h in screen_text:
                return {"text": h, "cx": 0, "cy": 0}
        return None
    b._find_hint = fake_find
    return b

def run(name, text, daily_expect, cooldown_expect):
    b = make_battler(text)
    d = b._check_daily_limit()
    b2 = make_battler(text)
    c = b2._check_rank_cooldown()
    ok = (d == daily_expect) and (c == cooldown_expect)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: daily_limit={d}(期望{daily_expect}) cooldown={c}(期望{cooldown_expect})")
    return ok

results = [
    run("30次上限弹窗", DAILY_TEXT, True, False),
    run("10把冷却弹窗", COOLDOWN_TEXT, False, True),
    run("普通主页", HUB_TEXT, False, False),
    run("上限+冷却同时出现(以daily_limit优先)", DAILY_TEXT + " 请休息15分钟", True, True),
]
print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
