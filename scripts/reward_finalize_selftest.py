"""开箱奖励落账自测 (2026-09-29)

覆盖 `Battler._finalize_reward` 的三条路径。为什么必须有这个用例:

  「开箱奖励」弹窗出现在结算链的 `end` 环节, 而本局胜负的判定时机**不确定** ——
  有时在该环节之前的**结算页标题**就判出来了 (留图时 verdict 已是 win/loss), 有时要等到
  `back` 环节的**战绩页大字**才判出来 (留图时还是 None)。第一版把"改 verdict"和"改名"写成
  同一个条件, 于是前一种情况整个分支被跳过、**改名也跟着被跳过**, 目录里一半文件带 `_win`
  一半是裸时间戳。这种 bug 靠真机复跑碰不到 (取决于那局走哪条路), 必须用桩数据锁死。

跑法: python scripts/reward_finalize_selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sj_bot.battler import Battler          # noqa: E402

PASS, FAIL = [], []


class _Cfg:
    def __init__(self, root: Path):
        self.capture_dir = root / "captures"
        self.data_dir = root / "data"


def _mk_battler(root: Path):
    """不跑 __init__ 造一个壳: 本用例只碰 _finalize_reward, 不需要设备/OCR/线程。"""
    b = object.__new__(Battler)
    b.cfg = _Cfg(root)
    b._logs = []
    b.log = lambda level, msg: b._logs.append((level, msg))
    (b.cfg.capture_dir / "rewards" / "20260929").mkdir(parents=True, exist_ok=True)
    (b.cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return b


def _seed(b, name: str, verdict: str):
    """造一条记录 + 对应图片, 返回 (相对路径, jsonl 路径)。"""
    day = b.cfg.capture_dir / "rewards" / "20260929"
    (day / name).write_bytes(b"\x89PNG\r\n\x1a\n")          # 内容无所谓, 只看文件在不在
    rel = str((day / name).relative_to(b.cfg.capture_dir))
    rp = b.cfg.data_dir / "rewards.jsonl"
    with rp.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-09-29 16:38:07", "mode": "rank", "round": 1,
                            "verdict": verdict, "file": rel}, ensure_ascii=False) + "\n")
    b._last_reward_file = rel
    return rel, rp


def _read(rp: Path) -> dict:
    return json.loads(rp.read_text(encoding="utf-8").splitlines()[0])


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <<< {detail}"))


def case(verdict_at_save: str, final: str, fname: str, expect_fname: str, title: str):
    root = Path(tempfile.mkdtemp(prefix="rwselftest_"))
    try:
        b = _mk_battler(root)
        rel, rp = _seed(b, fname, verdict_at_save)
        b._finalize_reward(final)
        rec = _read(rp)
        on_disk = [p.name for p in (b.cfg.capture_dir / "rewards" / "20260929").iterdir()]
        print(f"[{title}]  verdict_at_save={verdict_at_save} -> file={rec['file']}  磁盘={on_disk}")
        check(f"{title}: verdict 最终为 {final}", rec["verdict"] == final, f"实得 {rec['verdict']}")
        check(f"{title}: 文件名应为 {expect_fname}", Path(rec["file"]).name == expect_fname,
              f"实得 {Path(rec['file']).name}")
        check(f"{title}: 文件确实存在且只有 1 个",
              sum(1 for p in (b.cfg.capture_dir / "rewards" / "20260929").iterdir()) == 1,
              f"实得 {on_disk}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# 段 A: 胜负已在结算页判出 —— 留图时 verdict 就是对的那种 (第一版正是在这里漏改名)
case("loss", "loss", "163807.png", "163807_loss.png", "A 结算页已判负")

# 段 B: 留图时还没判出 —— 靠战绩页补判 (这一条第一版就是对的)
case("unknown", "win", "164055.png", "164055_win.png", "B 战绩页补判胜")

# 段 C: 幂等 —— 已经带对后缀、verdict 也对, 再调一次不该变成 "_win_win"
root = Path(tempfile.mkdtemp(prefix="rwselftest_"))
try:
    b = _mk_battler(root)
    rel, rp = _seed(b, "164055_win.png", "win")
    b._last_reward_file = rel
    b._finalize_reward("win")
    rec = _read(rp)
    names = [p.name for p in (b.cfg.capture_dir / "rewards" / "20260929").iterdir()]
    print(f"[C 幂等]  file={rec['file']}  磁盘={names}")
    check("C 幂等: 文件名不被二次加后缀", Path(rec["file"]).name == "164055_win.png",
          f"实得 {Path(rec['file']).name}")
    check("C 幂等: 磁盘上仍是 1 个文件", len(names) == 1, f"实得 {names}")

    # 段 D: 没有本局留图 (例如这局没走到 end 环节) -> 必须静默不动, 不写坏 jsonl
    b2 = _mk_battler(root)
    _, rp2 = _seed(b2, "170000.png", "unknown")
    b2._last_reward_file = None
    before = rp2.read_text(encoding="utf-8")
    b2._finalize_reward("win")
    after = rp2.read_text(encoding="utf-8")
    print(f"[D 无留图] jsonl 未变动={before == after}")
    check("D 无留图: jsonl 原样不动", before == after)
    check("D 无留图: 无任何日志", len(b2._logs) == 0, f"实得 {b2._logs}")
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
print(f"结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
