# -*- coding: utf-8 -*-
"""真机验证: 从"无法识别的页面"按系统返回键逃生 (只读 + 按键, 不进战斗/不消耗资源).

用途: 当前设备若停在副本关卡/排行榜等未知页, 本脚本会调用引擎的通用逃生
`_escape_unknown_page` 并打印每一步的页面识别结果, 同时存 before/after 截图.

用法: python scripts/live_escape_test.py
"""
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.battler import Battler            # noqa: E402


def main() -> None:
    b = Battler(mode="rank", rounds=1)
    b.log = lambda lv, msg: print(f"  [{lv}] {msg}")
    img = b._shot_img()
    if img is None:
        print("截图失败 (adb 未连接?)")
        return
    cv2.imwrite(str(ROOT / "captures" / "live_escape_before.png"), img)
    page0 = b._classify_page(img)
    print(f"逃生前页面识别: {page0}")
    if page0 in Battler._ESCAPE_OK_PAGES:
        print(f"当前已在可续跑页面 ({page0}), 无需逃生")
        return
    landed = b._escape_unknown_page("live_verify")
    print(f"逃生结果: {landed}")
    img2 = b._shot_img()
    if img2 is not None:
        cv2.imwrite(str(ROOT / "captures" / "live_escape_after.png"), img2)
        print(f"逃生后页面识别: {b._classify_page(img2)}")


if __name__ == "__main__":
    main()
