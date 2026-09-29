"""用「挑战统计」面板 114 名"曾赢过我"的对手, 反向核对「挑战记录」是否漏人。

数据来源: captures/stat_page_00..11.png 视觉直读 (列表按"败场数"降序,
第 11 页第 5 行起 败场数=0, 故前 114 行 = 曾经赢过我的全部对手)。

结论口径: 只要在统计面板里 败场数>=1, 就是"打不过"的客观证据
(不再要求"首次交锋即负"这种更严的口径)。
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sj_bot.config import Config  # noqa: E402
from sj_bot.db import OpponentDB  # noqa: E402

# (对手名, 我败场数) —— 挑战统计面板前 114 行, 按面板顺序
STAT = [
    ("恻隐的血大帝", 5), ("暗影的高天使", 4), ("隐身的炎机械", 4), ("冷却的血利刃", 3),
    ("冥界的白剑圣", 3), ("纳什猎天使", 3), ("圣光的黄游神", 3), ("残暴的青机械", 3),
    ("风暴的善战士", 3), ("重生的帅军神", 3),
    ("钢铁的影少女", 2), ("极寒的棕拳王", 2), ("隐身的小彗星", 2), ("郁闷的紫星辰", 2),
    ("永燃的富勇士", 2), ("勇士好奇心", 2), ("异界的大军神", 2), ("伟大的善法典", 2),
    ("无邪的棕流星", 2), ("小丑女主播", 2),
    ("战魂操纵者", 2), ("坠落的幻勇士", 2), ("祖安的幻彗星", 2), ("华瑶风行者", 2),
    ("黄昏的血怒吼", 2), ("复仇的睡狮", 2), ("符文的炎女神", 2), ("暗堕天使", 2),
    ("暗夜的邪离子", 2), ("超神的月末刃", 2),
    ("东方的恶幽魂", 2), ("命运的名真眼", 2), ("冥火的褐豺狼", 2), ("绝对的穷天马", 2),
    ("金世界树", 2), ("坚毅的碧霸者", 2), ("侯爵木乃伊", 1), ("自恋的白执事", 1),
    ("晓天的天灾", 1), ("嗜血的影枢纽", 1),
    ("图腾流离者", 1), ("月光的血皎月", 1), ("善弓箭手", 1), ("白小龙女", 1),
    ("不祥的恶狼人", 1), ("残暴的黑杀拳", 1), ("必胜的橙卡牌", 1), ("必胜的绯王者", 1),
    ("必中的月游神", 1), ("白鸟的影魔龙", 1),
    ("暗夜的王者", 1), ("东方的日轮", 1), ("灯塔龙斩者", 1), ("大武道家", 1),
    ("超神的绯冥王", 1), ("传奇的紫骑士", 1), ("纯白的蛮王", 1), ("俯视的炎狂蟒", 1),
    ("辅助风行者", 1), ("腐蚀的影剑魔", 1),
    ("激斗的小法典", 1), ("疾步的炎琴瑟", 1), ("疾风的帅蛮王", 1), ("杀手暗执事", 1),
    ("沙皇小红帽", 1), ("闪现的赛亚人", 1), ("圣洁的红弯刀", 1), ("破天的金少年", 1),
    ("扭曲的小奎因", 1), ("千钰龙斩者", 1),
    ("勤勉的虚灵", 1), ("日炎的高猎手", 1), ("箭羽三只手", 1), ("巨人安魂曲", 1),
    ("飓风的小富豪", 1), ("觉醒的棕化身", 1), ("狂欢的金盖伦", 1), ("困倦的绿兽人", 1),
    ("兰兔女郎", 1), ("莲花的暗义贼", 1),
    ("魔装的帅魔剑", 1), ("纳凉的火牙", 1), ("逆行的光华遥", 1), ("御风的黑刺客", 1),
    ("郁闷的橙夜刃", 1), ("永夜的绯锐刃", 1), ("永夜的兰旋风", 1), ("永生的小少年", 1),
    ("永生的棕刀锋", 1), ("英豪好胜心", 1),
    ("影流的独角兽", 1), ("虚无的恶魔", 1), ("妖精女主播", 1), ("野性的黄火牙", 1),
    ("王者鬼武者", 1), ("碎裂的圣剑圣", 1), ("温馨的赤亚索", 1), ("嗜血的恶剑魔", 1),
    ("淘气的王者", 1), ("天才的帅双鱼", 1),
    ("小斗牛士", 1), ("晓天的古君主", 1), ("仙灵吉格斯", 1), ("无常的橙霸者", 1),
    ("无双的古羽翼", 1), ("无双的萌凤凰", 1), ("无双的紫娜美", 1), ("自由的名皇帝", 1),
    ("最终的战神", 1), ("真理的紫千鸟", 1),
    ("镇压的穷杀手", 1), ("智将守护者", 1), ("仲裁的萌武神", 1), ("重生的绯战神", 1),
]

ZONE = re.compile(r"^\s*\d+区\s*")

# 统计面板不显示区号, 而库内名单一律存"NN区 名字"全名 ⇒ 写库时必须补区号。
# 来源: 挑战记录逐页转录的同名行 (scripts/arena_record_apply.py)。
ZONE_OF = {
    "不祥的恶狼人": "1区",
    "钢铁的影少女": "38区",
    "嗜血的影枢纽": "5区",
    "残暴的黑杀拳": "5区",
    "晓天的天灾": "8区",
}


def bare(n: str) -> str:
    return ZONE.sub("", n).strip()


def main() -> None:
    cfg = Config()
    db = OpponentDB(cfg.db_path)
    rows = db._conn.execute("SELECT name, verdict FROM opponents").fetchall()
    have = {}          # 去区号 -> 库内全名
    for r in rows:
        have.setdefault(bare(r["name"]), r["name"])

    miss, matched = [], []
    for nm, lose in STAT:
        if nm in have:
            matched.append((nm, have[nm], lose))
        else:
            cand = difflib.get_close_matches(nm, list(have), n=1, cutoff=0.75)
            if cand:
                matched.append((nm, have[cand[0]], lose))
            else:
                miss.append((nm, lose))

    print(f"挑战统计「曾赢过我」 = {len(STAT)} 人")
    print(f"  → 库内已有(含近字匹配) {len(matched)} 人")
    print(f"  → 库内没有, 需补录     {len(miss)} 人")
    if miss:
        print("\n--- 需补录名单 ---")
        for i, (nm, lose) in enumerate(miss, 1):
            print(f"  {i:2d}. {nm}   (我败 {lose})")

    st = db.stats()
    print(f"\n当前库: 名单 {st['cannot_beat']} 人")

    if "--write" not in sys.argv:
        print("\n[dry-run] 未写库; 加 --write 落库")
        db.close()
        return

    # 落库前先备份
    import shutil
    from datetime import datetime

    src = Path(cfg.db_path)
    bak = src.with_name(src.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M"))
    shutil.copy2(src, bak)
    print(f"\n已备份 -> {bak.name}")

    for nm, lose in miss:
        full = f"{ZONE_OF[nm]} {nm}" if nm in ZONE_OF else nm
        db.mark_no(full, reason=f"arena_stat:ever_lost(败{lose})")
        print(f"  + {full}")
    after = db.stats()
    print(f"\n写库完成: 名单 {st['cannot_beat']} -> {after['cannot_beat']} "
          f"(+{after['cannot_beat'] - st['cannot_beat']})")
    out = db.export_lists()
    print("名单导出 ->", out["cannot_beat"])
    db.close()


if __name__ == "__main__":
    main()
