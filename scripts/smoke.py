"""冒烟脚本: 设备探测 -> 连接 -> 截图 -> 控件树结构探测。

用法:
  python scripts/smoke.py                     # 自动选唯一在线设备
  python scripts/smoke.py --serial SKSCIF...  # 指定设备

结构探测的意义:
  游戏界面大多自绘(Unity/OpenGL), dump 出的控件树若几乎无文本节点,
  说明无法走控件方案, 需要视觉识别 —— 这一步是"识别方案"的前置判断。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from sj_bot.config import Config  # noqa: E402
from sj_bot.device import Device, list_serial  # noqa: E402


def main() -> int:
    cfg = Config()
    cfg.ensure_dirs()
    parser = argparse.ArgumentParser(description="圣剑挂机骨架冒烟验证")
    parser.add_argument("--serial", default=None, help="设备串号")
    args = parser.parse_args()

    print("== 1/4 adb 探测 ==")
    print("adb:", cfg.adb_exe)
    serials = list_serial(cfg.adb_exe)
    if not serials:
        print("[X] 没有在线设备。请先 USB 连接手机并允许调试, 或 adb connect <ip>。")
        return 1
    for s in serials:
        print("  在线:", s)

    serial = args.serial or (serials[0] if len(serials) == 1 else None)
    if not serial:
        print("[X] 多台设备, 请用 --serial 指定其中一台。")
        return 1

    print("\n== 2/4 连接设备 (首次会自动部署 ATX agent, 可能耗时) ==")
    try:
        dev = Device(serial=serial, config=cfg)
    except Exception as e:  # noqa: BLE001
        print("[X] 连接失败:", e)
        return 1
    info = dev.info()
    print("  已连接:", serial)
    for k in ("model", "brand", "sdk", "udid"):
        if k in info:
            print("   %s: %s" % (k, info[k]))

    print("\n== 3/4 截图 ==")
    shot = dev.save_shot("smoke")
    print("  已保存:", shot)

    print("\n== 4/4 控件树结构探测 ==")
    try:
        xml = dev.dump_xml(save=True)
        st = dev.hierarchy_stats(xml)
        print("  节点总数:", st["node_count"])
        print("  文本节点:", st["text_node_count"])
        print("  文本样例:", st["text_samples"])
        if st["text_node_count"] >= 3 and any(st["text_samples"]):
            print("  结论: 当前界面暴露了文本节点, 控件方案有望可用。")
        else:
            print("  结论: 几乎无文本节点 => 疑似自绘界面, 后续走视觉识别方案。")
    except Exception as e:  # noqa: BLE001
        print("  dump 失败(可能是权限/界面原因):", e)

    print("\n冒烟完成。当前停留在设备当前界面, 未做任何点击操作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
