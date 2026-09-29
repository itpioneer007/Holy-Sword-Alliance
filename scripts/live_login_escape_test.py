# -*- coding: utf-8 -*-
"""真机验证: 登录页识别 + 「开始」坐标 + 登录后进入主界面的过程 (只读/单次点击).

用途: 用户需求 (09-23) —— 登录页需点「开始」进游戏, 进入后可能还要按一次 back 才回到
主界面; 同时验证主城内「设置浮层」这类未知页能否一次 back 退回。

分阶段运行, 默认只做**只读诊断**, 加 --go 才真正点击:
  phase classify : 只截图 + 分类 + 存图 (不改任何状态)
  --go tap       : 点 LOGIN_START_COORD 并逐帧观测登录后落点

用法:
  python scripts/live_login_escape_test.py
  python scripts/live_login_escape_test.py --go tap
"""
import argparse
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot import rank_layout as rk          # noqa: E402
from sj_bot.battler import Battler            # noqa: E402


def _save(img, path: Path) -> None:
    """⚠️ 本机 ROOT 路径含中文, cv2.imwrite 会**静默失败** (返回 False 不抛异常),
    必须走 imencode + tofile —— 与 battler._save_anomaly 同一写法。
    注意 `tofile()` **成功时返回 None**, 不能用返回值判成败 (踩过一次), 用 exists 判。"""
    try:
        cv2.imencode(".png", img)[1].tofile(str(path))
    except Exception as e:
        print(f"  ⚠ 存图异常: {path.name}: {e}")
        return
    if not path.exists():
        print(f"  ⚠ 存图失败(文件未生成): {path.name}")


def shot(b: Battler, name: str):
    img = b._shot_img()
    if img is None:
        print("  截图失败")
        return None
    _save(img, ROOT / "outputs" / f"live_{name}.png")
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", default="", help="'tap' = 点击「开始」并观测登录过程")
    args = ap.parse_args()

    b = Battler(mode="rank", rounds=1)
    b.log = lambda lv, msg: print(f"  [{lv}] {msg}")

    img = shot(b, "phase0_before")
    if img is None:
        return
    print(f"帧尺寸: {img.shape[:2]}")

    t0 = time.time()
    is_login = b._is_login_screen(img)
    print(f"_is_login_screen       = {is_login}   ({time.time()-t0:.1f}s)")

    t0 = time.time()
    is_session = b._is_session_expired(img)
    print(f"_is_session_expired    = {is_session} ({time.time()-t0:.1f}s)")

    t0 = time.time()
    page = b._classify_page(img)
    print(f"_classify_page         = {page}        ({time.time()-t0:.1f}s)")

    # 「开始」按钮坐标处裁一小块, 供人工核对按钮是否正好在常量位置上
    sx, sy = rk.LOGIN_START_COORD
    half = 90
    crop = img[max(0, sy - half):sy + half, max(0, sx - half):sx + half].copy()
    _save(crop, ROOT / "outputs" / "live_start_btn_crop.png")
    print(f"LOGIN_START_COORD      = {rk.LOGIN_START_COORD}  (裁图 outputs/live_start_btn_crop.png)")

    if args.go != "tap":
        print("\n(只读模式结束; 加 --go tap 才会真正点击)")
        return

    # 连拍模式: 点击后**不做任何 OCR**, 只按固定间隔存帧 —— 全图 OCR 单次 10~25s,
    # 边拍边判会把采样间隔拉到几十秒, 直接把"登录后哪个瞬间需要 back"这段过程盖掉。
    # 先拍全, 再离线逐帧判 (或人工看图), 时间轴才干净。
    print(f"\n>>> 点击「开始」@ {rk.LOGIN_START_COORD} 然后连拍 (间隔 2s, 共 40 帧)")
    b._tap(*rk.LOGIN_START_COORD)
    t0 = time.time()
    for i in range(1, 41):
        time.sleep(2)
        img = b._shot_img()
        if img is None:
            print(f"  frame {i:02d} 截图失败")
            continue
        _save(img, ROOT / "outputs" / f"live_seq_{i:02d}.png")
        print(f"  frame {i:02d}  t+{time.time()-t0:5.1f}s  ok")
    print("连拍结束: outputs/live_seq_*.png")


if __name__ == "__main__":
    main()
