"""全民争霸「挑战记录」逐页采集。

面板为轮播式翻页: 左右两侧垂直居中各有一个菱形箭头 (实测 1600x900)。
  左箭头 PREV = (374, 413)   右箭头 NEXT = (1232, 412)
流程: 先连点左箭头回到第 1 页, 再逐页右翻; 直到面板文字指纹不再变化(末页)。
每页落盘 captures/rec_page_NN.png, 供离线解析。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sj_bot.config import Config  # noqa: E402

CAP = ROOT / "captures"
PANEL_ROI = (350, 185, 1250, 735)   # 面板数据区(含 10 行)
TIME_STRIP = (400, 185, 580, 735)   # 时间列: 无动画干扰, 作为"是否真的翻页"的指纹源
TOAST_ROI = (540, 150, 1130, 225)   # 顶部提示条: "已经是最后一页了" / "已经是第一页了"
PREV_PT = (374, 413)                # 面板左侧菱形箭头
NEXT_PT = (1232, 412)               # 面板右侧菱形箭头
MAX_PAGES = 400
CLICK_WAIT = 1.2
REWIND_TRIES = 12


class _Dev:
    def __init__(self) -> None:
        cfg = Config()
        self.adb = cfg.adb_exe
        self.serial = cfg.serial or "127.0.0.1:16384"
        subprocess.run([self.adb, "connect", self.serial],
                       capture_output=True)

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


def fingerprint(png: Path, roi: tuple = TIME_STRIP) -> str:
    """时间列像素指纹(缩小灰度后取哈希)。

    不要用整个面板区: 面板背景有流动光效/宝石动画, 会永远"变化",
    导致末页被反复当成新页(2026-09-11 曾因此在末页空转 100+ 次)。
    """
    import numpy as np
    from PIL import Image

    im = Image.open(png).convert("L").crop(roi).resize((90, 50))
    return hashlib.md5(np.array(im).tobytes()).hexdigest()


def toast_text(png: Path) -> str:
    """读取顶部提示条文字 (RapidOCR)。

    注意: 提示条("已经是第一页了"/"已经是最后一页了")是**瞬时**弹出的,
    1~2 秒后会淡出, 所以它只能当辅助判据; 主判据是时间列指纹冻结。
    """
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    if _OCR[0] is None:
        _OCR[0] = RapidOCR()
    im = Image.open(png).crop(TOAST_ROI)
    im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
    res, _ = _OCR[0](np.array(im))
    return "".join(t[1] for t in res) if res else ""


def is_last_toast(msg: str) -> bool:
    return "最后" in msg


def is_first_toast(msg: str) -> bool:
    """首页提示 OCR 常丢掉'一'(实测 '已经是第页了'), 故放宽匹配。"""
    return "第一" in msg or ("已经" in msg and "第" in msg and "页" in msg and "最后" not in msg)


_OCR = [None]


def main() -> None:
    dev = _Dev()
    tmp = CAP / "_tmp_rec.png"
    resume = "--resume" in sys.argv

    if resume:
        done = sorted(CAP.glob("rec_page_*.png"))
        done = [p for p in done if "_roi" not in p.name]
        start = len(done)
        print(f"断点续采: 已有 {start} 页, 从第 {start} 页继续")
    else:
        # 回到第 1 页: 连点左箭头, 出现"已经是第一页了"或指纹不变即停
        last = None
        for _ in range(REWIND_TRIES):
            dev.shot(tmp)
            msg = toast_text(tmp)
            if is_first_toast(msg):
                print(f"检测到首页提示『{msg}』")
                break
            fp = fingerprint(tmp)
            if fp == last:
                break
            last = fp
            dev.tap(*PREV_PT)
            time.sleep(CLICK_WAIT)
        dev.shot(tmp)
        print("已回到首页, fp =", fingerprint(tmp)[:10])
        start = 0

    # 逐页右翻采集: 出现"已经是最后一页了"提示即停, 不再点右箭头
    pages: list[Path] = []
    prev_fp = None
    for i in range(start, MAX_PAGES):
        dst = CAP / f"rec_page_{i:02d}.png"
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
        # 末页弹窗检测: 命中则本页已是最后一页, 立即停止(不要再点箭头)
        msg = toast_text(dst)
        if is_last_toast(msg):
            print(f"检测到末页提示『{msg}』-> 停止翻页, 共 {i + 1} 页")
            break
        dev.tap(*NEXT_PT)
        time.sleep(CLICK_WAIT)

    print("本次新增:", [p.name for p in pages])

    from PIL import Image

    for p in pages:
        Image.open(p).crop(PANEL_ROI).save(p.with_name(p.stem + "_roi.png"))


if __name__ == "__main__":
    main()
