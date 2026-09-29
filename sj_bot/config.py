"""集中配置: 路径与运行参数。

约定:
  - SJ_ADB_EXE 环境变量可覆盖 adb.exe 路径
  - SJ_SERIAL  环境变量可覆盖默认设备串号
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录


def default_adb() -> Optional[str]:
    """确定 adb.exe 路径: 环境变量 > 本机已知位置 > PATH。"""
    env = os.environ.get("SJ_ADB_EXE")
    if env:
        return env
    known = Path("D:/SoftwareDownload/platform-tools/adb.exe")
    if known.exists():
        return str(known)
    return shutil.which("adb")


class Config:
    def __init__(self) -> None:
        self.adb_exe: Optional[str] = default_adb()
        self.serial: Optional[str] = os.environ.get("SJ_SERIAL") or None

        self.data_dir: Path = BASE_DIR / "data"
        self.db_path: Path = self.data_dir / "sj_bot.db"
        self.list_dir: Path = self.data_dir / "lists"
        self.capture_dir: Path = BASE_DIR / "captures"
        self.template_dir: Path = BASE_DIR / "assets" / "templates"

        # 游戏包名 (2026-09-13 新增): 引擎用它做"游戏是否还在前台/进程是否还活着"
        # 的存活探测。MuMu 会在内存吃紧时把游戏进程回收或切到桌面, 而引擎此前
        # 完全没有这个概念 —— 所有判据只认游戏内页面, 于是"桌面"被当成 unknown,
        # 空等满 200s 匹配超时后终止 (实测白烧 4 分钟 0 场)。
        self.game_pkg: str = os.environ.get("SJ_GAME_PKG") or "com.holyblade.sjlm.ganxt"

        # 防挂机答题用 LLM (DeepSeek). 环境变量 SJ_LLM_KEY 优先, 其次此默认值.
        self.llm_key: str = os.environ.get("SJ_LLM_KEY") or "sk-e29ff1e5d19a44e58dec6c14724c0471"
        self.llm_base: str = os.environ.get("SJ_LLM_BASE") or "https://api.deepseek.com"
        self.llm_model: str = os.environ.get("SJ_LLM_MODEL") or "deepseek-chat"
        # 视觉模型名: 官方 api.deepseek.com 无 vision 模型; 若走第三方网关(模型别名如
        # deepseek-pro/deepseek-v4-flash-vision-exp)必须显式配 SJ_LLM_VISION_MODEL +
        # SJ_LLM_BASE 指向该网关, 否则别让请求悄悄打去别处. 默认别名仅作占位.
        self.llm_vision_model: str = os.environ.get("SJ_LLM_VISION_MODEL") or "deepseek-v4-flash-vision-exp"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.list_dir, self.capture_dir, self.template_dir):
            d.mkdir(parents=True, exist_ok=True)
