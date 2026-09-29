"""设备控制层: 封装 uiautomator2 的连接 / 截图 / 点击 / 控件树探测。

关键点:
  - 在 import uiautomator2 之前把 adb.exe 所在目录注入 PATH, 否则 u2.connect 找不到 adb。
  - dump_xml + hierarchy_stats 是"结构探测"入口: 游戏界面能否走控件方案,
    由文本节点数量判断; 几乎无文本节点 => 自绘界面, 后续走 vision 层。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from .config import Config

_TAG_RE = re.compile(r"<node\b")
_TEXT_RE = re.compile(r'text="([^"]+)"')


def _inject_adb_into_path(adb_exe: Optional[str]) -> None:
    if adb_exe:
        d = str(Path(adb_exe).resolve().parent)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def list_serial(adb_exe: Optional[str] = None) -> list[str]:
    """返回当前在线设备串号列表 (adb devices)。"""
    exe = adb_exe or Config().adb_exe
    if not exe:
        raise RuntimeError("未找到 adb.exe, 请设置 SJ_ADB_EXE 环境变量")
    out = subprocess.run(
        [exe, "devices"], capture_output=True, text=True, timeout=20
    ).stdout
    serials: list[str] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "offline" in line:
            continue
        serials.append(line.split()[0])
    return serials


class Device:
    """单设备封装。serial 为空时自动选择唯一在线设备。"""

    def __init__(
        self,
        serial: Optional[str] = None,
        adb_exe: Optional[str] = None,
        config: Optional[Config] = None,
    ) -> None:
        self.cfg = config or Config()
        self.cfg.ensure_dirs()
        adb = adb_exe or self.cfg.adb_exe
        if not adb:
            raise RuntimeError("未找到 adb.exe, 请设置 SJ_ADB_EXE 环境变量")
        _inject_adb_into_path(adb)

        self.serial = serial or self.cfg.serial
        if not self.serial:
            serials = list_serial(adb)
            if not serials:
                raise RuntimeError("没有在线设备, 请先连接手机/模拟器")
            if len(serials) > 1:
                raise RuntimeError(
                    "检测到多台设备 %s, 请用 --serial 指定" % serials
                )
            self.serial = serials[0]

        import uiautomator2 as u2  # 延迟导入, 确保 PATH 已注入

        self.d = u2.connect(self.serial)

    # ---------- 基础信息 ----------
    def info(self) -> dict:
        try:
            return self.d.device_info
        except Exception:
            return {"serial": self.serial}

    # ---------- 操作 ----------
    def click(self, x: int, y: int, timeout: float = 3.0) -> None:
        self.d.click(x, y)
        self.d.wait(timeout)

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.2
    ) -> None:
        self.d.swipe(x1, y1, x2, y2, duration=duration)

    def screenshot(self, save_path: Optional[Path] = None):
        """截图。save_path 为空则只返回 PIL Image。"""
        img = self.d.screenshot()
        if save_path is not None:
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(str(save_path))
        return img

    def save_shot(self, tag: str = "shot") -> Path:
        """按时间戳命名截图并落盘, 返回路径。"""
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.cfg.capture_dir / f"{ts}_{tag}.png"
        self.screenshot(path)
        return path

    # ---------- 结构探测 (判断是否自绘界面的关键) ----------
    def dump_xml(self, save: bool = False) -> str:
        xml = self.d.dump_hierarchy()
        if save:
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            p = self.cfg.capture_dir / f"{ts}_hierarchy.xml"
            p.write_text(xml, encoding="utf-8")
        return xml

    @staticmethod
    def hierarchy_stats(xml: str) -> dict:
        """统计控件树: 节点总数 / 带文本节点数 / 文本样例。

        信号含义:
          text_nodes 较大 => 界面可走控件方案 (dump/点击文本);
          text_nodes 很小且非空文本为空 => 疑似自绘, 需要视觉方案。
        """
        nodes = len(_TAG_RE.findall(xml))
        texts = [t for t in _TEXT_RE.findall(xml) if t.strip()]
        return {
            "node_count": nodes,
            "text_node_count": len(texts),
            "text_samples": texts[:8],
        }
