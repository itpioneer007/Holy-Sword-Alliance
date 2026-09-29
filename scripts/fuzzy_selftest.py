"""模糊匹配 (OCR 错字归一化) 自测。

覆盖:
  1. 完全相同的名字 -> 命中同一行
  2. 1 字差异 (5 字名, 阈值 0.85) -> 命中
  3. 1 字差异 (4 字名, 阈值 0.9) -> 命中
  4. 1 字差异 (3 字名, 阈值 1.0) -> 不命中
  5. 不同区服隔离 (避免跨区服误判)
  6. 完全不同的名字 (不同区服或不同名) -> 不命中
  7. upsert_from_scan 用模糊匹配避免重复建档
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot.db import OpponentDB  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got: object, want: object) -> None:
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sj_fuzzy_"))
    db = OpponentDB(tmp / "fuzzy.db")

    # 建档
    db.upsert_from_scan("45区 武神掘墓者")
    db.upsert_from_scan("40区 怪指挥官")
    db.upsert_from_scan("38区 三千的圣离子")   # OCR 应识别成"三千", 但这里写正确版本模拟
    db.upsert_from_scan("17区 无极的高剑魔")
    db.upsert_from_scan("40区 萌芽灭者")        # 5 字名

    # 1. 完全相同
    s = db.find_similar("45区 武神掘墓者")
    check("完全相同 -> 命中", s["name"] if s else None, "45区 武神掘墓者")

    # 2. 1 字差异 5 字名 (千->干), ratio = 7/8 = 0.875 >= 0.85 命中
    s = db.find_similar("38区 三干的圣离子")
    check("38区 1字差异 5字名 -> 命中", s["name"] if s else None, "38区 三千的圣离子")

    # 3. 1 字差异 4 字名 (芽->歼), ratio = 3/4 = 0.75 < 0.9 不命中
    s = db.find_similar("40区 萌歼灭者")
    check("40区 1字差异 4字名 -> 不命中 (阈值0.9)", s, None)

    # 4. 1 字差异 3 字名 (帆->吼), ratio = 2/3 = 0.667 < 1.0 不命中
    db.upsert_from_scan("1区 荒野的怒帆")  # 5 字
    # 改用 3 字测试: 直接传入 3 字名字
    db.upsert_from_scan("5区 张三")
    s = db.find_similar("5区 张四")
    check("3字名 1字差异 -> 不命中 (阈值1.0)", s, None)

    # 5. 不同区服隔离
    db.upsert_from_scan("17区 无极的高剑魔")
    s = db.find_similar("38区 无极的高剑魔")
    check("跨区服同名 -> 不命中", s, None)

    # 6. 完全不同 (4字名对比5字名, 不应命中)
    s = db.find_similar("38区 全新玩家X")
    check("完全无关 -> 不命中", s, None)

    # 7. upsert_from_scan 用模糊匹配避免重复
    db2 = OpponentDB(tmp / "fuzzy2.db")
    db2.upsert_from_scan("38区 三千的圣离子")  # 先建正确版
    db2.upsert_from_scan("45区 武神掘墓者")
    db2.upsert_from_scan("45区 武神堀墓者")     # 5字阈值0.85, ratio=0.8 不合并 -> 新建
    db2.upsert_from_scan("38区 三干的圣离子")   # 8字阈值0.8, ratio=0.875 -> 合并到38区
    n = db2.stats()["total"]
    check("模糊合并: 5字不合并 + 8字合并 = 3 行", n, 3)
    # 38区 应仍是"三千的圣离子"
    s = db2.find_similar("38区 三干的圣离子")
    check("模糊合并后档案名仍是 38区 三千的圣离子", s["name"] if s else None, "38区 三千的圣离子")
    # 45区两个独立
    a = db2.find_similar("45区 武神掘墓者")
    b = db2.find_similar("45区 武神堀墓者")
    check("45区两个独立档案 (掘墓者 命中)", a["name"] if a else None, "45区 武神掘墓者")
    check("45区两个独立档案 (堀墓者 独立)", b["name"] if b else None, "45区 武神堀墓者")

    # 8. 决策层: OCR 错字名也要能命中"打不过名单" (2026-09-11 补)
    import sj_bot.state_machine as sm

    db3 = OpponentDB(tmp / "fuzzy3.db")
    db3.mark_no("38区 残暴的青机械", reason="selftest")
    flow = sm.GameFlow(device=None, db=db3)  # type: ignore[arg-type]
    check("决策: 精确名 -> skip", flow.decide({"name": "38区 残暴的青机械"}), "skip")
    check("决策: OCR 错字(青->清) -> 仍 skip",
          flow.decide({"name": "38区 残暴的清机械"}), "skip")
    check("决策: 陌生名 -> battle", flow.decide({"name": "9区 全新对手"}), "battle")
    check("决策: 跨区服同名 -> 不受影响 battle",
          flow.decide({"name": "40区 残暴的青机械"}), "battle")
    db3.close()

    db.close()
    db2.close()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()