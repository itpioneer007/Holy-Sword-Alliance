"""档案库自测: 建档 -> 胜负回写(连败2次定性) -> 名单 -> 导出。

规则 (2026-09-08 纯黑名单模型):
  - win   -> wins+1, 连败清零, verdict='beat'(曾战胜战绩, 不参与决策)
  - loss  -> losses+1, 连败+1; 连续 >=2 次才 verdict='no'
             (首败保留原 verdict, 防单局偶发误伤)
  - skip  -> 外部预判打不过(如排行采集), 仅对从未战胜者生效;
             曾战胜(beat)者被 skip 不降级

运行: python scripts/db_selftest.py
仅依赖标准库 (sqlite3), 不需要真机。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from sj_bot.db import OpponentDB  # noqa: E402

PASS = 0
FAIL = 0


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]", msg)
    else:
        FAIL += 1
        print("  [FAIL]", msg)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        db = OpponentDB(db_path=Path(td) / "test.db")

        print("== 场景 1: 列表扫描建档 ==")
        xm = db.upsert_from_scan("小明", rating=5000, level=40)
        check(xm["wins"] == 0 and xm["verdict"] == "unknown", "新对手小明建档, verdict=unknown")
        xg = db.upsert_from_scan("小刚", rating=9000, level=60)
        check(xg["rating"] == 9000, "新对手小刚建档, 战力 9000")

        print("== 场景 2: 打赢小明 -> beat 战绩 ==")
        xm = db.record("小明", "win", rating=5000, level=40)
        check(xm["wins"] == 1 and xm["verdict"] == "beat", "小明 wins=1, verdict=beat")
        check(xm["lose_streak"] == 0, "小明连败计数清零")
        check(len(db.list_by_verdict("beat")) == 1, "曾战胜记录含小明")

        print("== 场景 3: 陌生首败 -> 观察中, 不立即定性 ==")
        xg = db.record("小刚", "loss")
        check(xg["losses"] == 1 and xg["lose_streak"] == 1, "小刚 losses=1, 连败=1")
        check(xg["verdict"] == "unknown", "首败保持 unknown(观察中), 不进打不过名单")
        check(len(db.list_by_verdict("no")) == 0, "打不过名单仍为空")

        print("== 场景 4: 二连败 -> 定性打不过 ==")
        xg = db.record("小刚", "loss")
        check(xg["lose_streak"] == 2 and xg["verdict"] == "no", "二连败后 verdict=no")
        check(len(db.list_by_verdict("no")) == 1, "打不过名单含小刚")

        print("== 场景 5: 打赢 -> 连败清零并移出打不过名单 ==")
        xg = db.record("小刚", "win", rating=9500)
        check(xg["verdict"] == "beat" and xg["lose_streak"] == 0, "打赢小刚, verdict=beat 且连败清零")
        check(len(db.list_by_verdict("no")) == 0, "打不过名单已清空小刚")

        print("== 场景 6: 曾战胜者同样受连败缓冲, 二连败也入名单 ==")
        xg = db.record("小刚", "loss")
        check(xg["verdict"] == "beat" and xg["lose_streak"] == 1, "曾胜者首败仍保持 beat(观察)")
        xg = db.record("小刚", "loss")
        check(xg["lose_streak"] == 2 and xg["verdict"] == "no", "曾胜者二连败同样定性 no")
        check(len(db.list_by_verdict("no")) == 1, "打不过名单再次含小刚")

        print("== 场景 7: skip = 排行采集预判 ==")
        xh = db.record("小黑", "skip")
        check(xh["verdict"] == "no", "从未战胜者被 skip 预判 -> 打不过名单")
        xm = db.record("小明", "skip")
        check(xm["verdict"] == "beat", "曾战胜者被 skip 不降级(仍 beat)")

        print("== 场景 8: 重复扫描不产生重复记录 ==")
        db.upsert_from_scan("小明", rating=5200)
        check(db.stats()["total"] == 3, "档案总数仍为 3(小明/小刚/小黑), 无重复建档")

        print("== 场景 9: 统计口径 (曾胜计数/打不过计数) ==")
        st = db.stats()
        check(st["ever_won"] == 1 and st["cannot_beat"] == 2,
              "曾胜 1 人(小明), 打不过 2 人(小刚/小黑), got=%s" % st)

        print("== 场景 10: 名单导出(仅打不过名单) ==")
        paths = db.export_lists(Path(td) / "lists")
        check("cannot_beat" in paths, "导出含 cannot_beat 文件")
        check("beat" not in paths, "不再导出 can_beat(能打名单)文件")
        text = paths["cannot_beat"].read_text(encoding="utf-8")
        check("打不过名单" in text, "cannot_beat 文件含表头")
        check("小刚" in text and "小黑" in text, "cannot_beat 文件含小刚/小黑")
        check("小明" not in text, "曾胜者小明不出现在打不过名单")

        db.close()

    print("\n统计: %s 通过, %s 失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
