"""规则落定后的决策层自测 (2026-09-08 纯黑名单模型)。

覆盖用户确认的判定语义:
  1. 陌生(unknown)对手 -> battle (不在打不过名单即打)
  2. 曾战胜(beat)战绩 -> battle (beat 不参与决策, 不拦路)
  3. 打不过名单(no)   -> skip (本次窗口一直跳过, 不再尝试)
  4. 首败不降级: 曾战胜/陌生输 1 场仍保持原状态 -> 继续 battle
  5. 连败 2 次定性打不过 -> 此后 skip
  6. skip(排行采集预判)从未战胜者 -> no -> skip
  7. 对手「修养中」30s: 刚打过的人 -> cooldown; 30s 过后恢复 battle
     (2026-09-17 用户口述修订: 原"同一玩家 10 分钟不可重复攻击"作废)
  8. 周积分上限: 6000 分 (每胜 80 -> 75 胜) 后 -> stop
  9. 时段闸门: 周六/周日 16:00-17:00 在窗口内, 其余不在 (2026-09-11 修订, 原 15:00)
 10. 名单导出: 仅落盘打不过名单(cannot_beat), 不再导出能打名单
 11. (2026-09-11 新规) 行动力机制常量: 单场间隔 0 / 恢复 60s / 每胜 80 分
"""
from __future__ import annotations

import datetime as _dt
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot.db import OpponentDB  # noqa: E402
from sj_bot import state_machine as sm  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got: object, want: object) -> None:
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="sj_bot_test_")
    db = OpponentDB(Path(tmp) / "test.db")
    flow = sm.GameFlow(device=None, db=db)  # type: ignore[arg-type]

    # --- 1. 陌生对手 -> battle ---
    check("陌生对手首战 battle", flow.decide({"name": "陌生人A"}), "battle")

    # --- 2/3. 曾战胜(beat) 打, 打不过(no) 跳过 ---
    db.record("能打赢的人", "win")
    db.record("打不过的人", "skip")  # 排行采集式预判 -> no
    check("曾战胜 -> battle", flow.decide({"name": "能打赢的人"}), "battle")
    check("打不过名单 -> skip", flow.decide({"name": "打不过的人"}), "skip")

    # --- 4/5. 曾胜者首败观察, 二连败才定性 ---
    db.record("能打赢的人", "loss")  # streak=1
    rec = db.get("能打赢的人")
    check("曾胜者首败 verdict 仍 beat", rec["verdict"], "beat")
    check("曾胜者首败后仍 battle", flow.decide({"name": "能打赢的人"}), "battle")
    db.record("能打赢的人", "loss")  # streak=2
    rec = db.get("能打赢的人")
    check("二连败后 verdict 转 no", rec["verdict"], "no")
    check("二连败后 skip", flow.decide({"name": "能打赢的人"}), "skip")

    # --- 6. skip 预判从未胜者 -> no -> skip ---
    db.record("预判强敌", "skip")
    check("skip 预判从未胜者 verdict=no", db.get("预判强敌")["verdict"], "no")
    check("skip 预判 -> skip 决策", flow.decide({"name": "预判强敌"}), "skip")

    # --- 7. 对手「修养中」30s (2026-09-17 用户口述修订) ---
    check("修养中窗口常量 = 30s", sm.ATTACK_COOLDOWN_SEC, 30)
    db.mark_attack("冷却测试人")
    check("刚打过 -> cooldown", flow.decide({"name": "冷却测试人"}), "cooldown")
    # 把攻击时间改成 31s 前 (修养结束) -> 恢复 battle
    old = (_dt.datetime.now() - _dt.timedelta(seconds=31)).isoformat(timespec="seconds")
    db._conn.execute("UPDATE opponents SET last_attacked_at = ? WHERE name = ?",
                     (old, "冷却测试人"))
    db._conn.commit()
    check("修养结束(31s)后恢复 battle", flow.decide({"name": "冷却测试人"}), "battle")

    # --- 8. 周积分上限 (每胜 80 分: 75 胜 = 6000 分) ---
    for i in range(75):
        db.record(f"刷分对手{i}", "win")
    check("75 胜后周积分 = 6000", db.weekly_win_points() >= 6000, True)
    check("周积分到 6000 上限 -> stop", flow.decide({"name": "能打赢的人"}), "stop")
    # 用全新库文件重建, 避免 6000 分残留影响后续判定
    db.close()
    db2 = OpponentDB(Path(tmp) / "test2.db")
    flow = sm.GameFlow(device=None, db=db2)  # type: ignore[arg-type]
    check("干净库陌生 -> battle", flow.decide({"name": "新人"}), "battle")

    # --- 9. 时段闸门 (2026-09-11: 16:00-17:00) ---
    sat_15 = _dt.datetime(2026, 9, 12, 15, 30)  # 周六 15:30 (改版后不在窗口)
    sat_16 = _dt.datetime(2026, 9, 12, 16, 0)   # 周六 16:00
    sun_16 = _dt.datetime(2026, 9, 13, 16, 0)   # 周日 16:00
    sun_17 = _dt.datetime(2026, 9, 13, 17, 0)   # 周日 17:00 (不含)
    mon_10 = _dt.datetime(2026, 9, 14, 10, 0)   # 周一
    fri_16 = _dt.datetime(2026, 9, 11, 16, 0)   # 周五 16:00
    check("周六 15:30 不在窗口(改版后)", sm.in_window(sat_15), False)
    check("周六 16:00 在窗口", sm.in_window(sat_16), True)
    check("周日 16:00 在窗口", sm.in_window(sun_16), True)
    check("周日 17:00 不在窗口", sm.in_window(sun_17), False)
    check("周一 10:00 不在窗口", sm.in_window(mon_10), False)
    check("周五 16:00 不在窗口", sm.in_window(fri_16), False)
    nxt = sm.next_open_start(mon_10)
    check("下个窗口从周一算起是下周六 16:00", nxt, _dt.datetime(2026, 9, 19, 16, 0))

    # --- 10. 导出仅打不过名单 ---
    out = db2.export_lists(Path(tmp) / "lists")
    check("导出键仅 cannot_beat", sorted(out.keys()), ["cannot_beat"])
    check("打不过名单文件可读", "打不过名单" in out["cannot_beat"].read_text(encoding="utf-8"), True)

    # --- 11. 荣耀时刻时段闸门 (每日 18-20) ---
    gl_1759 = _dt.datetime(2026, 9, 8, 17, 59)   # 周二
    gl_1800 = _dt.datetime(2026, 9, 8, 18, 0)
    gl_1930 = _dt.datetime(2026, 9, 8, 19, 30)
    gl_2000 = _dt.datetime(2026, 9, 8, 20, 0)   # 不含
    check("荣耀时刻 17:59 不在", sm.in_glory_window(gl_1759), False)
    check("荣耀时刻 18:00 在", sm.in_glory_window(gl_1800), True)
    check("荣耀时刻 19:30 在 (任意日)", sm.in_glory_window(gl_1930), True)
    check("荣耀时刻 20:00 不在", sm.in_glory_window(gl_2000), False)
    check("荣耀时刻周中同样开放(无 weekend 限制)", sm.in_glory_window(_dt.datetime(2026, 9, 9, 18, 30)), True)

    # --- 12. 行动力机制常量 (2026-09-11 新规: 取代 30s 单场 CD) ---
    check("单场间隔 0 (有行动力连打)", sm.ARENA_INTERVAL_SEC, 0)
    check("兼容旧名 BATTLE_INTERVAL_SEC 同步为 0", sm.BATTLE_INTERVAL_SEC, 0)
    check("行动力耗尽后 60s 一场", sm.STAMINA_RECOVER_SEC, 60)
    check("每次挑战耗 1 点行动力", sm.STAMINA_COST_PER_BATTLE, 1)
    check("行动力上限 20", sm.STAMINA_MAX, 20)
    check("每胜 80 分 (唯一口径 db.POINTS_PER_WIN)", sm.POINTS_PER_WIN, 80)
    check("积分常量与 db 一致", sm.POINTS_PER_WIN, __import__("sj_bot.db", fromlist=["x"]).POINTS_PER_WIN)
    check("周上限 6000 = 75 胜", 6000 // sm.POINTS_PER_WIN, 75)

    db2.close()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
