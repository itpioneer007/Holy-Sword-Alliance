"""荣耀排行批量采集引擎 (可被 CLI 脚本或 Web 控制台复用)。

从当前荣耀排行页开始, 翻 pages 页, 每页 OCR 解析玩家并写入"打不过"名单。
- log: 日志回调 (level, msg) 或 BotLog 实例
- stop_evt: threading.Event, set 后优雅停止(当前页结算后退出)
- 复用单个 RapidOCR 实例提速 (避免每页重复加载模型)
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from sj_bot.config import Config
from sj_bot.db import OpponentDB

# 实测坐标 (1600x900)
NEXT_PAGE = (875, 775)   # 下一页 "❯"
PREV_PAGE = (730, 775)   # 上一页 "❮"
LIST_REGION = (150, 170, 1500, 700)  # x0,y0,x1,y1 名单区
PAGE_WAIT = 2.5          # 翻页后等待刷新(秒)

TIER_WORDS = {"宗师", "王者", "大师", "钻石", "铂金", "黄金", "白银", "青铜", "星耀", "传说"}
ROW_PAT = re.compile(r"(\d{1,3})\s*区\s*([\u4e00-\u9fffA-Za-z]{2,12})")


def _adb_cmd(adb: str, serial: str, args: list[str]) -> None:
    subprocess.run([adb, "-s", serial, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RankCollector:
    def __init__(self, pages: int = 40, log: Optional[Callable] = None,
                 stop_evt: Optional[threading.Event] = None) -> None:
        self.pages = pages
        self.log = log or (lambda *_: None)
        self.stop_evt = stop_evt or threading.Event()
        self.cfg = Config()
        self.serial = self.cfg.serial or "127.0.0.1:16384"
        self.adb = self.cfg.adb_exe
        self._ocr = None
        self._db: Optional[OpponentDB] = None
        self.cancelled = False

    # ---- 懒加载重型对象 ----
    @property
    def ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self._ocr = RapidOCR()
        return self._ocr

    @property
    def db(self) -> OpponentDB:
        if self._db is None:
            self._db = OpponentDB(self.cfg.db_path)
        return self._db

    # ---- 工具 ----
    def _screenshot(self, dst: Path) -> bool:
        try:
            subprocess.run(f'"{self.adb}" connect {self.serial} >nul 2>&1', shell=True)
            r = subprocess.run(
                [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                capture_output=True,
            )
            if r.returncode != 0 or not r.stdout:
                return False
            dst.write_bytes(r.stdout)
            return True
        except Exception:
            return False

    def _tap(self, x: int, y: int) -> None:
        _adb_cmd(self.adb, self.serial, ["shell", "input", "tap", str(x), str(y)])

    @staticmethod
    def _imread(p: Path) -> np.ndarray:
        buf = np.fromfile(p, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(p)
        return img

    def _extract_players(self, img: np.ndarray) -> list[tuple[str, str]]:
        """OCR 单页排行 -> [(区服, 名字), ...]"""
        x0, y0, x1, y1 = LIST_REGION
        roi = img[y0:y1, x0:x1]
        result, _ = self.ocr(roi)
        if not result:
            return []

        def y_top(box):
            ys = [p[1] for p in box if isinstance(p, (list, tuple)) and len(p) >= 2
                  and isinstance(p[1], (int, float))]
            return min(ys) if ys else 0

        def x_left(box):
            xs = [p[0] for p in box if isinstance(p, (list, tuple)) and len(p) >= 1
                  and isinstance(p[0], (int, float))]
            return min(xs) if xs else 0

        raw = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            box, text = item[0], item[1]
            if not isinstance(box, (list, tuple)) or len(box) < 4:
                continue
            if isinstance(text, str) and text.strip():
                raw.append((box, text.strip()))

        items = sorted(raw, key=lambda it: (y_top(it[0]), x_left(it[0])))
        rows: list[list] = []
        for it in items:
            if rows and abs(y_top(it[0]) - y_top(rows[-1][0][0])) < 15:
                rows[-1].append(it)
            else:
                rows.append([it])

        players: list[tuple[str, str]] = []
        for row in rows:
            parts = sorted(row, key=lambda r: x_left(r[0]))
            line = " ".join(p[1] for p in parts)
            m = ROW_PAT.search(line)
            if not m:
                continue
            zone = f"{m.group(1)}区"
            name = m.group(2)
            if name in TIER_WORDS:
                continue
            players.append((zone, name))
        seen: set[tuple[str, str]] = set()
        return [p for p in players if not (p in seen or seen.add(p))]

    # ---- 主流程 ----
    def run(self) -> dict:
        self.log("info", f"荣耀排行采集开始: 目标 {self.pages} 页 (每页约10人 → 打不过名单)")
        total_seen = 0
        started = time.time()
        try:
            for page in range(1, self.pages + 1):
                if self.stop_evt.is_set():
                    self.cancelled = True
                    self.log("warn", "收到停止信号, 当前页完成后退出")
                    break
                shot = Path(self.cfg.capture_dir) / f"_collect_{page}.png"
                if not self._screenshot(shot):
                    self.log("error", f"第 {page} 页截图失败, 中止")
                    break
                try:
                    img = self._imread(shot)
                    players = self._extract_players(img)
                finally:
                    try:
                        shot.unlink()
                    except OSError:
                        pass
                n = 0
                for zone, name in players:
                    row = self.db.upsert_from_scan(f"{zone} {name}")
                    self.db.record(row["name"], "skip")
                    n += 1
                total_seen += len(players)
                self.log("info", f"第 {page:>2}/{self.pages} 页: 识别 {len(players):>2} 人, "
                                 f"新入打不过名单 {n} 人 (累计识别 {total_seen})")
                if page < self.pages:
                    self._tap(*NEXT_PAGE)
                    time.sleep(PAGE_WAIT)
            st = self.db.stats()
            out = self.db.export_lists(self.cfg.list_dir)
            cost = time.time() - started
            self.log("ok", f"采集{'中止' if self.cancelled else '完成'}! 档案总数 {st['total']} "
                           f"(曾胜 {st['ever_won']} / 打不过 {st['cannot_beat']}), "
                           f"名单文件 {out['cannot_beat'].name}, 耗时 {cost:.0f}s")
            return {"total_seen": total_seen, "stats": st, "file": str(out["cannot_beat"]),
                    "cancelled": self.cancelled}
        finally:
            if self._db is not None:
                self._db.close()


def run_cli() -> int:
    """命令行入口: python -m sj_bot.collector [页数]"""
    import sys

    def _log(level: str, msg: str) -> None:
        print(f"[{level}] {msg}", flush=True)

    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    RankCollector(pages=pages, log=_log).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
