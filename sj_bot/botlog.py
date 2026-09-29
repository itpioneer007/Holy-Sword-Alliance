"""线程安全的环形日志, 供后台任务与 Web 端共享。

2026-09-29 变更: **增加按日落盘**
    在此之前本模块**没有任何文件写入** —— 日志只活在内存环形缓冲里, 控制台进程
    一重启就全部失忆。后果在 09-29 的优化评估里暴露得很彻底: `reports/` 里最新
    一份运行日志停在 09-23, 09-24~09-28 六天的过程日志**完全不存在**, 于是
    "09-28 那场为什么 19 局只赢 4 局" 这个问题**根本无法复盘** —— 只剩
    `data/job_history.jsonl` 里一行收尾摘要。

    现在每次 append 同时追加到 `data/logs/bot-YYYY-MM-DD.log` (按日一个文件),
    并在进程启动时把**当天文件的尾部**读回环形缓冲, 这样刷新页面就能看到重启前
    的日志, 而不是从空白开始。

设计取舍
    · 不引入 logging 模块: 本模块要同时服务"内存环形缓冲 + 增量 seq 拉取"这个
      Web 轮询协议, 用 logging 反而要绕回自定义 Handler 去同步 _seq。
    · 落盘失败**绝不抛异常**: 控制台的首要职责是跑任务, 磁盘满/权限异常不能让
      玩家掉线。失败时静默降级为"仅内存", 但只提示一次。
"""
from __future__ import annotations

import datetime as _dt
import re
import threading
from collections import deque
from pathlib import Path
from typing import Optional

# 落盘行的格式: 2026-09-29 15:30:00 [info] 消息...
# 用完整日期而不是 HH:MM:SS —— 日志文件跨天可读, 且回填时能校验日期。
_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$")


class BotLog:
    def __init__(self, maxlen: int = 800, file_dir: Optional[Path] = None) -> None:
        self._maxlen = maxlen
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._file_dir: Optional[Path] = Path(file_dir) if file_dir else None
        self._fh = None
        self._fh_day: Optional[str] = None
        self._file_broken = False

    # ---------------------------------------------------------------- 落盘
    def _file_for_today(self):
        """拿到今天的日志文件句柄 (跨天自动切换)。返回 None 表示不可用。"""
        if self._file_dir is None or self._file_broken:
            return None
        day = _dt.date.today().strftime("%Y-%m-%d")
        if self._fh is not None and self._fh_day == day:
            return self._fh
        try:
            self._file_dir.mkdir(parents=True, exist_ok=True)
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
            self._fh = (self._file_dir / f"bot-{day}.log").open(
                "a", encoding="utf-8", errors="replace")
            self._fh_day = day
            return self._fh
        except OSError:
            self._file_broken = True          # 只坏一次, 之后走纯内存
            return None

    def _write(self, ts_full: str, level: str, msg: str) -> None:
        fh = self._file_for_today()
        if fh is None:
            return
        try:
            fh.write(f"{ts_full} [{level}] {msg}\n")
            fh.flush()                        # 崩溃前也要留下现场, 1 行 flush 成本可忽略
        except OSError:
            self._file_broken = True

    # ---------------------------------------------------------------- 写入
    def append(self, level: str, msg: str) -> None:
        now = _dt.datetime.now()
        ts = now.strftime("%H:%M:%S")
        with self._lock:
            self._seq += 1
            self._items.append({"seq": self._seq, "ts": ts, "level": level, "msg": msg})
            self._write(now.strftime("%Y-%m-%d %H:%M:%S"), level, msg)

    def info(self, msg: str) -> None:
        self.append("info", msg)

    def ok(self, msg: str) -> None:
        self.append("ok", msg)

    def warn(self, msg: str) -> None:
        self.append("warn", msg)

    def error(self, msg: str) -> None:
        self.append("error", msg)

    # ---------------------------------------------------------------- 读取
    def lines(self, after_seq: int = 0, limit: int = 200) -> tuple[int, list[dict]]:
        """返回 (当前最新 seq, 增量行列表[旧->新])。after_seq=0 取最近 limit 条。"""
        with self._lock:
            items = list(self._items)
            cur = self._seq
        if after_seq:
            out = [i for i in items if i["seq"] > after_seq]
        else:
            out = items[-limit:]
        return cur, out

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def log_file(self) -> Optional[Path]:
        return None if self._fh_day is None or self._file_dir is None \
            else self._file_dir / f"bot-{self._fh_day}.log"

    # ---------------------------------------------------------------- 启动回填
    def load_recent(self, n: int = 200) -> int:
        """把今天日志文件的**尾部 n 行**读回环形缓冲, 返回载入行数。

        为什么只读今天: 昨天的日志属于"历史", 混进今天的实时流里会让 Web 端
        时间线错乱; 需要昨天的直接读文件。
        """
        if self._file_dir is None:
            return 0
        day = _dt.date.today().strftime("%Y-%m-%d")
        p = self._file_dir / f"bot-{day}.log"
        if not p.exists():
            return 0
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                tail = deque(f, maxlen=n)
        except OSError:
            return 0

        loaded = 0
        with self._lock:
            for line in tail:
                m = _LINE_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                full, level, msg = m.groups()
                self._seq += 1
                self._items.append({"seq": self._seq, "ts": full[11:], "level": level,
                                    "msg": msg + "  (重启前)"})
                loaded += 1
        return loaded
