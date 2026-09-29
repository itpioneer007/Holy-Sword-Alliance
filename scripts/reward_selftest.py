"""开箱奖励链自测 (2026-09-29) —— 识别 + 落账, 一个入口跑完。

覆盖两个容易出错的环节:

【A. 图标识别】`sj_bot/reward_icons`
  弹窗只画图标不写文字, 物品名只能靠图标认。判据是"颜色为主 (用户给的口径), 模板兜底"。
  这里用 `assets/reward_icons/ref/` 里固化的 4 份真实 ROI 做回归 —— 样本放 assets 而不是
  captures, 因为 captures/ 有例行清理 (2026-09-23 nav_retry 自测就因样本被清而长期崩)。
  另加**合成扰动**段 (换数量 / 改亮度 / 加噪声), 因为手上每类只有 1 帧真实样本,
  靠扰动才能覆盖"同一物品换个数量"这个必然发生的场景。

【B. 落账与改名】`Battler._finalize_reward`
  胜负判定时机不确定: 有时在结算页标题就判出 (留图时 verdict 已是 win/loss), 有时要等到
  战绩页大字才判出 (留图时还是 None)。第一版把"改 verdict"与"改名"写成同一个条件, 于是前
  一种情况整个分支被跳过、**改名也跟着被跳过**。这种 bug 靠真机复跑碰不到 (取决于那局走哪条
  路), 必须用桩数据锁死。文件名规则后来改成了 `<HHMMSS>_<物品名>_<胜负>.png`。

跑法: python scripts/reward_selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sj_bot.battler import Battler                      # noqa: E402
from sj_bot.reward_icons import (                       # noqa: E402
    ICON_DIR, REWARD_ICON_MASK, REWARD_ICON_ROI, UNKNOWN_NAME,
    load_templates, match_icon, match_roi,
)

PASS: list[str] = []
FAIL: list[str] = []

# 4 份真实样本 -> 期望物品名 (2026-09-29 与用户人工确认的口径对齐)
REF_EXPECT = {
    "162226_win.png":  "副本扫荡券",    # 金卷轴「扫」
    "162456_win.png":  "紫色经验卡",    # 紫色卡牌 + 药水瓶
    "163807_loss.png": "钻石",          # 蓝青宝石
    "164055_win.png":  "装备扫荡券",    # 紫卷轴「扫」
}

REF_DIR = ICON_DIR / "ref"


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <<< {detail}"))


def load_ref(fn: str) -> np.ndarray | None:
    p = REF_DIR / fn
    if not p.exists():
        return None
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def bright(img: np.ndarray, factor: float) -> np.ndarray:
    """亮度缩放。H 不变、V 变 —— 专门用来压测"银灰"这个带亮度门槛的判据。"""
    return np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def noisy(img: np.ndarray, sigma: float, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)                  # 固定种子: 自测必须可复现
    n = rng.normal(0, sigma, img.shape)
    return np.clip(img.astype(np.float32) + n, 0, 255).astype(np.uint8)


# ================================================================ A. 识别

print("=== A. 图标识别 ===")
refs = {fn: load_ref(fn) for fn in REF_EXPECT}
missing = [fn for fn, v in refs.items() if v is None]
if missing:
    print(f"  跳过: 缺回归样本 {missing} (先跑 python scripts/diag_reward_icons.py --emit)")
else:
    print("-- A1 真实样本 (含 ×N 角标) 直接识别")
    for fn, want in REF_EXPECT.items():
        got, conf, src = match_roi(refs[fn])
        check(f"A1 {fn} -> {want}", got == want, f"实得 {got} (置信 {conf:.2f}, 判据 {src})")

    print("-- A2 合成扰动 (换数量 / 亮度 / 噪声) 后仍应认出")
    x0, y0, x1, y1 = REWARD_ICON_MASK
    keys = list(REF_EXPECT)
    for i, fn in enumerate(keys):
        want = REF_EXPECT[fn]
        base = refs[fn]
        # 换数量: 把自己右下角换成另一物品的角标区 (比单纯改数字更剧烈)
        donor = refs[keys[(i + 1) % len(keys)]]
        swapped = base.copy()
        swapped[y0:y1, x0:x1] = donor[y0:y1, x0:x1]
        for tag, img in (("换角标", swapped),
                         ("亮度-12%", bright(base, 0.88)),
                         ("亮度+12%", bright(base, 1.12)),
                         ("噪声σ8", noisy(base, 8))):
            got, conf, src = match_roi(img)
            check(f"A2 {fn} {tag} -> {want}", got == want,
                  f"实得 {got} (置信 {conf:.2f}, 判据 {src})")

    print("-- A3 整帧路径 match_icon (ROI 裁剪 + 角标屏蔽都在生产函数里)")
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = (60, 70, 80)                          # 近似战斗背景色
    rx0, ry0, rx1, ry1 = REWARD_ICON_ROI
    for fn, want in REF_EXPECT.items():
        f2 = frame.copy()
        f2[ry0:ry1, rx0:rx1] = refs[fn]
        got, conf, src = match_icon(f2)
        check(f"A3 {fn} 整帧 -> {want}", got == want, f"实得 {got} (置信 {conf:.2f})")
    # 帧太小 -> 不许瞎认
    got, _c, src = match_icon(np.zeros((100, 100, 3), np.uint8))
    check("A3 帧过小 -> 待命名 (不猜)", got == UNKNOWN_NAME, f"实得 {got}")
    check("A3 帧过小 -> 判据记 无", src == "无", f"实得 {src}")

    print("-- A4 模板兜底 (模拟颜色失效: 整帧转灰度)")
    # 灰度化后所有色相占比归零 -> 颜色判据必然失败 -> 只剩模板兜底
    for fn, want in REF_EXPECT.items():
        g = cv2.cvtColor(refs[fn], cv2.COLOR_BGR2GRAY)
        g3 = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
        got, conf, src = match_roi(g3)
        check(f"A4 {fn} 灰度 -> {want} (走模板)", got == want,
              f"实得 {got} (置信 {conf:.2f}, 判据 {src})")

    print("-- A5 绝不猜: 无意义图一律 待命名")
    rng = np.random.default_rng(11)
    for tag, img in (("纯噪声", rng.integers(0, 255, (172, 192, 3), dtype=np.uint8)),
                     ("纯黑", np.zeros((172, 192, 3), np.uint8)),
                     ("纯白", np.full((172, 192, 3), 255, np.uint8))):
        got, conf, src = match_roi(np.ascontiguousarray(img))
        check(f"A5 {tag} -> 待命名", got == UNKNOWN_NAME, f"实得 {got} (置信 {conf:.2f})")

print()


# ================================================================ B. 落账与改名

print("=== B. 落账与改名 (_finalize_reward) ===")


class _Cfg:
    def __init__(self, root: Path):
        self.capture_dir = root / "captures"
        self.data_dir = root / "data"


def _mk_battler(root: Path):
    """不跑 __init__ 造一个壳: 本用例只碰 _finalize_reward, 不需要设备/OCR/线程。"""
    b = object.__new__(Battler)
    b.cfg = _Cfg(root)
    b._logs = []
    b.log = lambda level, msg: b._logs.append((level, msg))
    (b.cfg.capture_dir / "rewards" / "20260929").mkdir(parents=True, exist_ok=True)
    (b.cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return b


def _seed(b, name: str, verdict: str, item: str | None = "钻石"):
    """造一条记录 + 对应图片, 返回 (相对路径, jsonl 路径)。item=None 模拟历史旧记录。"""
    day = b.cfg.capture_dir / "rewards" / "20260929"
    (day / name).write_bytes(b"\x89PNG\r\n\x1a\n")          # 内容无所谓, 只看文件在不在
    rel = str((day / name).relative_to(b.cfg.capture_dir))
    rec = {"ts": "2026-09-29 16:38:07", "mode": "rank", "round": 1,
           "verdict": verdict, "file": rel}
    if item is not None:
        rec["item"] = item
        rec["item_conf"] = 0.9
        rec["item_src"] = "颜色"
    rp = b.cfg.data_dir / "rewards.jsonl"
    with rp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    b._last_reward_file = rel
    return rel, rp


def _read(rp: Path) -> dict:
    return json.loads(rp.read_text(encoding="utf-8").splitlines()[0])


def _files(b) -> list[str]:
    return [p.name for p in (b.cfg.capture_dir / "rewards" / "20260929").iterdir()]


def case(verdict_at_save: str, final: str, fname: str, item: str, expect: str, title: str):
    root = Path(tempfile.mkdtemp(prefix="rwselftest_"))
    try:
        b = _mk_battler(root)
        _seed(b, fname, verdict_at_save, item)
        b._finalize_reward(final)
        rec = _read(rp_of(b))
        on_disk = _files(b)
        print(f"[{title}]  verdict@{verdict_at_save} -> {rec['file']}  磁盘={on_disk}")
        check(f"{title}: verdict 最终为 {final}", rec["verdict"] == final, f"实得 {rec['verdict']}")
        check(f"{title}: 文件名应为 {expect}", Path(rec["file"]).name == expect,
              f"实得 {Path(rec['file']).name}")
        check(f"{title}: 磁盘上只有 1 个文件", len(on_disk) == 1, f"实得 {on_disk}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def rp_of(b) -> Path:
    return b.cfg.data_dir / "rewards.jsonl"


# B1: 胜负已在结算页判出 —— 留图时 verdict 就是对的 (第一版正是在这里漏改名)
case("loss", "loss", "163807.png", "钻石", "163807_钻石_loss.png", "B1 结算页已判负")
# B2: 留图时还没判出 —— 靠战绩页补判
case("unknown", "win", "164055.png", "装备扫荡券", "164055_装备扫荡券_win.png", "B2 战绩页补判胜")
# B3: 历史旧记录没有 item 字段 -> 名字里用「待命名」占位, 不许崩
case("win", "win", "170000.png", None, "170000_待命名_win.png", "B3 旧记录无 item")

# B4: 幂等 —— 已经改名到位, 再调一次不该变成 "_win_win"
root = Path(tempfile.mkdtemp(prefix="rwselftest_"))
try:
    b = _mk_battler(root)
    _seed(b, "164055_装备扫荡券_win.png", "win", "装备扫荡券")
    b._finalize_reward("win")
    rec = _read(rp_of(b))
    names = _files(b)
    print(f"[B4 幂等]  file={rec['file']}  磁盘={names}")
    check("B4 幂等: 文件名不变", Path(rec["file"]).name == "164055_装备扫荡券_win.png",
          f"实得 {Path(rec['file']).name}")
    check("B4 幂等: 磁盘仍只有 1 个文件", len(names) == 1, f"实得 {names}")

    # B5: 没有本局留图 (这局没走到 end 环节) -> 必须静默不动, 不写坏 jsonl
    b2 = _mk_battler(root)
    _, rp2 = _seed(b2, "180000.png", "unknown", "钻石")
    b2._last_reward_file = None
    before = rp2.read_text(encoding="utf-8")
    b2._finalize_reward("win")
    after = rp2.read_text(encoding="utf-8")
    print(f"[B5 无留图] jsonl 未变动={before == after}")
    check("B5 无留图: jsonl 原样不动", before == after)
    check("B5 无留图: 无任何日志", len(b2._logs) == 0, f"实得 {b2._logs}")
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---- 模板库状态 (顺带体检: 缺模板不报错, 但要让人看见) ----
print()
have = {n for n, _c, _t in load_templates(force=True)}
want_all = {"副本扫荡券", "装备扫荡券", "紫色经验卡", "钻石", "普通经验符文"}
print(f"模板库: {len(have)} 个 -> {'、'.join(sorted(have)) or '空'}")
lack = want_all - have
if lack:
    print(f"  缺: {'、'.join(sorted(lack))}  (颜色判据仍认得出, 模板只是兜底)")
print()

print(f"结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("  - " + f)
sys.exit(1 if FAIL else 0)
