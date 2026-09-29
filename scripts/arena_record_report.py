"""生成「挑战记录 -> 打不过名单」导入报告, 并刷新名单导出文件。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.arena_record_apply import B_INCLUDE, load  # noqa: E402
from sj_bot.config import Config  # noqa: E402
from sj_bot.db import OpponentDB  # noqa: E402


def main() -> None:
    recs = load()
    by_opp: dict[str, list[dict]] = {}
    for r in recs:
        by_opp.setdefault(r["opp"], []).append(r)

    first_loss, bclass, pure = [], [], []
    for opp, rows in by_opp.items():
        if not any(r["res"] == "L" for r in rows):
            pure.append(opp)
        elif rows[0]["res"] == "L":
            first_loss.append(opp)
        else:
            bclass.append(opp)
    inc = sorted(first_loss) + sorted(o for o in B_INCLUDE)
    # 第二栏只列"确实未入名单"的: 排除已被 B_INCLUDE 纠正入列的
    exc = sorted(o for o in bclass if o not in B_INCLUDE)

    cfg = Config()
    db = OpponentDB(cfg.db_path)
    st = db.stats()
    out = db.export_lists()

    def seq(opp: str) -> str:
        return " → ".join(f"{r['ts']} {r['res']}" for r in by_opp[opp])

    L: list[str] = []
    L.append("# 全民争霸「挑战记录」→ 打不过名单 导入报告")
    L.append("")
    L.append("导入时间: 2026-09-11   |   数据源: 游戏内「挑战记录」面板, 共 **36 页 / 360 场**"
             " (09/05 15:55 – 09/06 16:20)")
    L.append("")
    L.append("- 我方游戏名: **38区 狂热的绯少女**")
    L.append("- 判定口径: ① 我主动挑战某人**第一次就输** ② 别人主动挑战我且**他赢了**")
    L.append("- 首战是我赢、后来才输的, 按字面口径**不入**名单 "
             "(已逐行核对「挑战方」列确认方向)")
    L.append("")
    L.append("## 结果总览")
    L.append("")
    L.append("| 项目 | 数量 |")
    L.append("| --- | --- |")
    L.append(f"| 解析对局总数 | {len(recs)} |")
    L.append(f"| 不同对手 | {len(by_opp)} |")
    L.append(f"| **本次判定入名单** | **{len(inc)}** |")
    L.append("| 其中新面孔 | 89 |")
    L.append("| 库内已有(同名) | 20 |")
    L.append(f"| 首战赢过、后输 (不入名单) | {len(exc)} |")
    L.append(f"| 从未输过 | {len(pure)} |")
    L.append(f"| 名单总数 (入库后) | {st['cannot_beat']} |")
    L.append("")
    L.append(f"## 一、本次写入的对手 ({len(inc)} 人)")
    L.append("")
    for i, n in enumerate(inc, 1):
        L.append(f"{i}. **{n}**  —  {seq(n)}")
    L.append("")
    L.append(f"## 二、首战赢过、后来才输 → 按字面口径未入名单 ({len(exc)} 人)")
    L.append("")
    L.append("> 若希望策略上更保险, 说一声即可补录。")
    L.append("")
    for n in exc:
        L.append(f"- {n}  —  {seq(n)}")
    L.append("")
    L.append(f"名单导出文件: `{out['cannot_beat']}` (共 {st['cannot_beat']} 人)")
    L.append("")

    # 报告属于"分析产物", 与 data/ 的运行期状态(名单/预约/额度)职责分开 —— 09-29 归位
    rep = ROOT / "reports" / "arena_record_report_20260911.md"
    rep.write_text("\n".join(L), encoding="utf-8")
    print("报告 ->", rep)
    print("名单导出 ->", out["cannot_beat"], "共", st["cannot_beat"], "人")


if __name__ == "__main__":
    main()
