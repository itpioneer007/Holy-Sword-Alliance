"""对手档案库 (SQLite 单文件)。

数据模型: 一个对手一行, 记录最近战力/等级快照与历史胜负。
verdict 是当前判定: no(打不过) / beat(曾战胜) / unknown(未见评估或首败观察中)。

决策语义 = 纯黑名单 (2026-09-08 用户确认):
  - 唯一闸门是"打不过名单"(verdict == 'no'); 不在名单内即可挑战,
    不存在独立维护的"能打名单";
  - beat 只是"曾战胜过"的战绩标签, 不参与决策、不导出成名单文件;
  - 实战战败(loss)需连续 2 次 (lose_streak >= 2) 才定性 no, 防单局偶发
    误伤; 中途赢一场立即清零连败计数;
  - skip 事件 = 外部预判打不过(荣耀排行采集把宗师/王者直接入档),
    仅对从未战胜过的记录生效; 曾战胜(beat)者被 skip 时不降级;
  - 对手被打后进入「修养中」(2026-09-17 用户口述: ~30s) 期间不可再挑战 (last_attacked_at);
  - 单场间隔: 2026-09-11 起改为"行动力机制" (有行动力连打, 耗尽后 1 场/分钟),
    见 state_machine.STAMINA_RECOVER_SEC, 旧"30s 节奏"已作废;
  - 每周六/日 16:00-17:00 窗口 (见 state_machine.in_window);
  - 每胜 +80 积分 (POINTS_PER_WIN), 周上限 6000 (events 表统计, 见 weekly_win_points)。

注意: 判定规则(什么条件算打不过)属于决策层, 在 state_machine 中实现;
这里只负责存储与查询。
"""
from __future__ import annotations

import csv
import datetime as _dt
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Optional

VerDict = Literal["beat", "no", "unknown"]

# 连续战败几次才定性"打不过"。2 = 首败观察、二连败入名单(防单局偶发)
LOSS_STREAK_TO_NO = 2

# 单场挑战胜利获得的兑换积分 (2026-09-11 游戏面板实测: 80; 原 20 作废)。
# 唯一口径放这里: state_machine 与 weekly_win_points 都引用本常量。
# 注: 名人模式下主动挑战"非名人"玩家胜利积分减半(40), 故按 80 累计得
# 到的周积分是保守上限(真实值只会更小), 用于"打够 6000 就收手"的闸门足够。
POINTS_PER_WIN = 80

_SCHEMA = """
CREATE TABLE IF NOT EXISTS opponents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    rating          INTEGER,                -- 最近一次看到的战力快照
    level           INTEGER,                -- 最近一次看到的等级
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    skips           INTEGER NOT NULL DEFAULT 0,
    lose_streak     INTEGER NOT NULL DEFAULT 0,  -- 当前连败次数(>=2 定性 no)
    verdict         TEXT    NOT NULL DEFAULT 'unknown',
    last_attacked_at TEXT,                  -- 最近一次主动挑战时间(对手修养中 30s)
    updated_at      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT    NOT NULL,               -- 事件时间(本地 ISO)
    kind    TEXT    NOT NULL,               -- 'win' / 'loss' / 'skip' / 'attack'
    name    TEXT    NOT NULL,
    detail  TEXT
);
"""

# 新增列的历史迁移(旧库缺列时 ALTER TABLE 补充)
_MIGRATIONS = [
    ("last_attacked_at", "TEXT"),
    ("lose_streak", "INTEGER NOT NULL DEFAULT 0"),
]


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


class OpponentDB:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else None
        if self.db_path is None:
            from .config import Config

            self.db_path = Config().db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """把 schema 演进为最新: 缺列则 ALTER TABLE ADD COLUMN。"""
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(opponents)").fetchall()
        }
        for col, decl in _MIGRATIONS:
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE opponents ADD COLUMN {col} {decl}"
                )

    # ---------- 基础操作 ----------
    def _split_zone_name(self, full: str) -> tuple[str, str]:
        """'45区 武神掘墓者' -> ('45区', '武神掘墓者'); 无区服时 ('', full)。"""
        parts = full.split(maxsplit=1)
        if len(parts) == 2 and parts[0].endswith("区"):
            return parts[0], parts[1]
        return "", parts[0]

    def _similarity_threshold(self, name_only: str) -> float:
        """按名字长度自适应: 短名严, 长名宽, 避免误伤/漏识别。"""
        n = len(name_only)
        if n <= 3:
            return 1.0
        if n == 4:
            return 0.9
        if n == 5:
            return 0.85
        return 0.8

    def find_similar(self, name: str, threshold: float | None = None) -> Optional[dict]:
        """在同区服档案中找名字最相似的记录。用于 OCR 错字归一化。

        name 形如 '45区 武神掘墓者' 或纯名字。threshold 默认按长度自动选。
        返回最相似的行(dict), 无相似返回 None。
        """
        zone, name_only = self._split_zone_name(name)
        if zone:
            rows = self._conn.execute(
                "SELECT * FROM opponents WHERE name LIKE ?",
                (f"{zone} %",),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM opponents WHERE name NOT LIKE '% %' OR name LIKE '%区 %'"
            ).fetchall()
            rows = [r for r in rows if self._split_zone_name(r["name"])[0] == ""]
        if not rows:
            return None
        thr = threshold if threshold is not None else self._similarity_threshold(name_only)
        best: dict | None = None
        best_ratio = 0.0
        for row in rows:
            existing = row["name"]
            _, e_name = self._split_zone_name(existing)
            ratio = SequenceMatcher(None, name_only, e_name).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = dict(row)
        return best if best and best_ratio >= thr else None

    def upsert_from_scan(
        self, name: str, rating: Optional[int] = None, level: Optional[int] = None
    ) -> dict:
        """在列表里见到对手即建档/刷新(先存档案, 判定在决策层做)。

        先做模糊匹配: 若已有同名同区服的近似档案, 直接 UPDATE 现有行
        (保留旧名字作溯源, 但避免重复建档)。
        """
        similar = self.find_similar(name)
        canonical = similar["name"] if similar else name
        now = _now()
        self._conn.execute(
            """
            INSERT INTO opponents (name, rating, level, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                rating = COALESCE(?, rating),
                level  = COALESCE(?, level),
                updated_at = ?
            """,
            (canonical, rating, level, now, now, rating, level, now),
        )
        self._conn.commit()
        row = self.get(canonical)
        assert row is not None
        return row

    def mark_attack(self, name: str) -> dict:
        """主动挑战开始前调用: 记录攻击时间, 用于对手「修养中」(30s) 过滤。"""
        now = _now()
        self._conn.execute(
            """
            INSERT INTO opponents (name, last_attacked_at, updated_at, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                last_attacked_at = ?, updated_at = ?
            """,
            (name, now, now, now, now, now),
        )
        self._conn.execute(
            "INSERT INTO events (ts, kind, name, detail) VALUES (?, 'attack', ?, NULL)",
            (now, name),
        )
        self._conn.commit()
        row = self.get(name)
        assert row is not None
        return row

    def record(
        self,
        name: str,
        result: Literal["win", "loss", "skip"],
        rating: Optional[int] = None,
        level: Optional[int] = None,
    ) -> dict:
        """战斗/评估后回写。verdict 派生规则 (纯黑名单模型):

        win  -> wins+1, 连败清零, verdict='beat'(曾战胜战绩)
        loss -> losses+1, 连败+1; 连续 >=2 次才 verdict='no',
                首败保留原 verdict(unknown/beat 均继续观察, 防单局偶发)
        skip -> skips+1; 外部预判打不过(如排行采集), 仅对从未战胜
                (verdict != 'beat') 的记录定性 no, 曾战胜者不降级
        """
        now = _now()
        if result == "win":
            inc = "wins = wins + 1, lose_streak = 0"
        elif result == "loss":
            inc = "losses = losses + 1, lose_streak = lose_streak + 1"
        else:
            inc = "skips = skips + 1"
        # 1) 计数 + 快照 upsert (SQL 中引用的列值均为更新前旧值)
        self._conn.execute(
            f"""
            INSERT INTO opponents (name, rating, level, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                rating      = COALESCE(?, rating),
                level       = COALESCE(?, level),
                {inc},
                updated_at  = ?
            """,
            (name, rating, level, now, now, rating, level, now),
        )
        # 2) 读回最新行, 纯 Python 派生 verdict (逻辑直白, 便于单测)
        row = dict(
            self._conn.execute(
                "SELECT * FROM opponents WHERE name = ?", (name,)
            ).fetchone()
        )
        if result == "win":
            nv: VerDict = "beat"
        elif result == "loss":
            nv = "no" if row["lose_streak"] >= LOSS_STREAK_TO_NO else row["verdict"]
        else:  # skip: 预判打不过; 曾战胜(beat)者不降级
            nv = "beat" if row["verdict"] == "beat" else "no"
        if nv != row["verdict"]:
            self._conn.execute(
                "UPDATE opponents SET verdict = ? WHERE name = ?", (nv, name)
            )
        self._conn.execute(
            "INSERT INTO events (ts, kind, name, detail) VALUES (?, ?, ?, NULL)",
            (now, result, name),
        )
        self._conn.commit()
        row = self.get(name)
        assert row is not None
        return row

    def mark_no(self, name: str, reason: str = "manual") -> dict:
        """直接把某人定性为打不过 (verdict='no'), 不要求连败 2 次。

        用于**数据来源可靠**的场景: 游戏内「挑战记录」的实战胜负、
        人工确认等。与 record(..., 'skip') 的区别是不做 beat 保护 ——
        战绩证据优先于旧的"曾战胜"标签。

        reason 写入 events.detail, 便于事后溯源(csv 导出/审计)。
        """
        now = _now()
        self._conn.execute(
            """
            INSERT INTO opponents (name, updated_at, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET updated_at = ?
            """,
            (name, now, now, now),
        )
        self._conn.execute(
            "UPDATE opponents SET verdict = 'no' WHERE name = ?", (name,)
        )
        self._conn.execute(
            "INSERT INTO events (ts, kind, name, detail) VALUES (?, 'no', ?, ?)",
            (now, name, reason),
        )
        self._conn.commit()
        row = self.get(name)
        assert row is not None
        return row

    def get(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM opponents WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def list_by_verdict(self, verdict: VerDict) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM opponents WHERE verdict = ? ORDER BY updated_at DESC",
            (verdict,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        total = self._conn.execute(
            "SELECT COUNT(*) FROM opponents"
        ).fetchone()[0]
        ever_won = self._conn.execute(
            "SELECT COUNT(*) FROM opponents WHERE verdict = 'beat'"
        ).fetchone()[0]
        cannot_beat = self._conn.execute(
            "SELECT COUNT(*) FROM opponents WHERE verdict = 'no'"
        ).fetchone()[0]
        return {
            "total": total,
            "ever_won": ever_won,        # 曾战胜过的人数(战绩标签, 不参与决策)
            "cannot_beat": cannot_beat,  # 打不过名单(决策唯一依据)
        }

    # ---------- 冷却与周积分 ----------
    def is_on_cooldown(self, name: str, cooldown_sec: int = 30) -> bool:
        """对手是否仍在自己打出的「修养中」窗口内 (默认 30s, 2026-09-17 用户口述)。

        注意: 这是**本地软过滤**, 只覆盖"本任务打过的场次"; 别人打的场次本地不可知,
        因此最终以列表上的按钮状态 (挑战 / 修养中) 为准 —— 见 battler._btn_still_challenge。
        """
        row = self.get(name)
        if not row or not row.get("last_attacked_at"):
            return False
        try:
            last = _dt.datetime.fromisoformat(row["last_attacked_at"])
        except ValueError:
            return False
        return (_dt.datetime.now() - last).total_seconds() < cooldown_sec

    def weekly_win_points(self, now: Optional[_dt.datetime] = None) -> int:
        """本周已获得的兑换积分(每胜 +POINTS_PER_WIN=80)。取本周一 00:00 起统计 win 事件。"""
        now = now or _dt.datetime.now()
        monday = (now - _dt.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        n = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'win' AND ts >= ?",
            (monday.isoformat(timespec="seconds"),),
        ).fetchone()[0]
        return n * POINTS_PER_WIN

    # ---------- 名单导出 ----------
    def export_lists(self, list_dir: Optional[Path] = None) -> dict[str, Path]:
        """把"打不过名单"(决策唯一依据)落盘为可读文件。

        不再导出"能打名单": beat 只是战绩标签, 见模块 docstring。
        返回 {"cannot_beat": Path}。
        """
        if list_dir is None:
            from .config import Config

            list_dir = Config().list_dir
        list_dir = Path(list_dir)
        list_dir.mkdir(parents=True, exist_ok=True)
        rows = self.list_by_verdict("no")
        p = list_dir / "cannot_beat"
        p.write_text(
            "# 打不过名单 (导出时间 %s, 共 %d 人; 决策只认此名单, 不在名单即可挑战)\n"
            % (_now(), len(rows)),
            encoding="utf-8",
        )
        with p.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["名字", "战力", "等级", "胜", "负", "跳", "最近更新"])
            for r in rows:
                writer.writerow(
                    [r["name"], r["rating"], r["level"],
                     r["wins"], r["losses"], r["skips"], r["updated_at"]]
                )
        return {"cannot_beat": p}

    def close(self) -> None:
        self._conn.close()
