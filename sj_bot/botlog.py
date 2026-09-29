"""线程安全的环形日志, 供后台任务与 Web 端共享。"""
from __future__ import annotations

import datetime as _dt
import threading
from collections import deque
from typing import Optional


class BotLog:
    def __init__(self, maxlen: int = 800) -> None:
        self._maxlen = maxlen
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, level: str, msg: str) -> None:
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._seq += 1
            self._items.append({"seq": self._seq, "ts": ts, "level": level, "msg": msg})

    def info(self, msg: str) -> None:
        self.append("info", msg)

    def ok(self, msg: str) -> None:
        self.append("ok", msg)

    def warn(self, msg: str) -> None:
        self.append("warn", msg)

    def error(self, msg: str) -> None:
        self.append("error", msg)

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
