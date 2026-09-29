"""全民争霸「挑战统计」逐页采集。

面板 = 按对手聚合的战绩表: 对手名字 | 发起挑战 | 被挑战 | 胜场数 | 败场数
按"败场数"降序排列, 所以第 1 页第 1 行 = 我输得最多的对手。

为什么需要它: 「挑战统计」是**按对手聚合**的战绩表 (对手名字 | 发起挑战 | 被挑战 |
胜场数 | 败场数), 按"败场数"降序 —— 也就是说, 前 N 行就是**曾经赢过我的全部对手**,
一行一个人, 不用像 360 条流水那样自己去重、判断方向。这是"打不过名单"最直接的证据源。

2026-09-11 实测结论 (1600x900, 一次周末窗口):
  - 统计面板共 248 个对手, 其中"败场数>=1"= **114 人**;
  - 挑战记录 36 页/360 条推出来的"输过的不同对手"也是 **114 人**, 两边**完全对上**;
  - 且 5 名被"首战即负"口径排除的人, 其统计数字(如 38区 钢铁的影少女 发起6/被挑战1/胜5/败2)
    与逐行转录的战绩 (W W L W W L W) **逐位吻合** —— 说明记录面板没漏掉任何一个赢过我的人,
    只可能漏掉我赢的场次(统计 248 人 vs 记录去重后 237 人)。

翻页: 与挑战记录同一套轮播组件, 左右菱形箭头 PREV=(374,413) NEXT=(1232,412)。
每页落盘 captures/stat_page_NN.png。

⚠️ 2026-09-11 踩坑: 点「挑战统计」按钮**不会**把面板重置到第一页 —— 面板会保留上次停
留的页。第一次采集因此从列表中途开始, 漏掉了前面的页。正确做法是先连点左箭头直到
顶部弹出「已经是第一页了」(左箭头 PREV, 与右箭头同为轮播组件), 再开始采集。
--rewind 参数会自动执行这个回退步骤。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sj_bot.config import Config  # noqa: E402

CAP = ROOT / "captures"
STAT_BTN = (622, 843)               # 面板底部「挑战统计」按钮 (实测)
NAME_COL = (390, 210, 800, 600)     # 对手名字列: 白色大字, 作为"是否真的翻页"指纹源
TOAST_ROI = (540, 150, 1130, 225)   # 顶部提示条: 「已经是第一页了」/「已经是最后一页了」
PREV_PT = (374, 413)
NEXT_PT = (1232, 412)
MAX_PAGES = 120
CLICK_WAIT = 1.3
REWIND_TRIES = 40


def toast_text(png: Path) -> str:
    """读取顶部提示条 (瞬时弹出, 仅作辅助判据)。"""
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    if _OCR[0] is None:
        _OCR[0] = RapidOCR()
    im = Image.open(png).crop(TOAST_ROI)
    im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
    res, _ = _OCR[0](np.array(im))
    return "".join(t[1] for t in res) if res else ""


_OCR = [None]


class _Dev:
    def __init__(self) -> None:
        cfg = Config()
        self.adb = cfg.adb_exe
        self.serial = cfg.serial or "127.0.0.1:16384"
        subprocess.run([self.adb, "connect", self.serial], capture_output=True)

    def tap(self, x: int, y: int) -> None:
        subprocess.run([self.adb, "-s", self.serial, "shell", "input", "tap",
                        str(x), str(y)], capture_output=True, check=True)

    def shot(self, dst: Path) -> bool:
        r = subprocess.run([self.adb, "-s", self.serial, "exec-out",
                            "screencap", "-p"], capture_output=True)
        if r.returncode != 0 or not r.stdout:
            return False
        dst.write_bytes(r.stdout)
        return True


def fingerprint(png: Path) -> str:
    """名字列的高亮像素掩码指纹 (阈值化, 抗半透明背景动画)。"""
    import hashlib
    import numpy as np
    from PIL import Image

    im = Image.open(png).convert("L").crop(NAME_COL)
    m = (np.asarray(im, dtype=np.uint8) > 150).astype(np.uint8)
    im2 = Image.fromarray(m * 255).resize((64, 48))
    return hashlib.md5(np.array(im2).tobytes()).hexdigest()


def main() -> None:
    dev = _Dev()
    tmp = CAP / "_tmp_stat.png"

    if "--rewind" in sys.argv:
        # 连点左箭头直到顶部提示「已经是第一页了」; 指纹不变也停(兜底)
        last = None
        for _ in range(REWIND_TRIES):
            dev.shot(tmp)
            msg = toast_text(tmp)
            if "第一" in msg or ("已经" in msg and "页" in msg and "最后" not in msg):
                print(f"检测到首页提示『{msg}』")
                break
            fp = fingerprint(tmp)
            if fp == last:
                print("指纹不再变化 -> 认为已到首页")
                break
            last = fp
            dev.tap(*PREV_PT)
            time.sleep(CLICK_WAIT)
        dev.shot(tmp)
        print("已回到首页, fp =", fingerprint(tmp)[:10])

    pages: list[Path] = []
    prev_fp = None
    for i in range(MAX_PAGES):
        dst = CAP / f"stat_page_{i:02d}.png"
        if not dev.shot(dst):
            print(f"第 {i} 页截图失败")
            break
        fp = fingerprint(dst)
        if fp == prev_fp:
            dst.unlink(missing_ok=True)
            print(f"第 {i} 页与上页相同 -> 已到末页, 共 {i} 页")
            break
        pages.append(dst)
        prev_fp = fp
        msg = toast_text(dst)
        if "最后" in msg:
            print(f"检测到末页提示『{msg}』-> 停止翻页, 共 {i + 1} 页")
            break
        dev.tap(*NEXT_PT)
        time.sleep(CLICK_WAIT)

    print("采集完成:", [p.name for p in pages])


if __name__ == "__main__":
    main()
