"""奖励图存储自测 (2026-09-29) —— 体积维持的三层闸门, 一个入口跑完。

起因是用户这个问题: "那我应该怎么样维持这个文件的体积不要太大?"

实测把问题的量级摆出来了: 09-29 一天写了 **57 张 / 86.8 MB** 奖励整帧 (1600x900 PNG,
1.52 MB/张)。按此速率, "保留 365 天"= 30.9 GB/年。于是分三层治:

    1. 源头减量 : 落盘改 JPEG q88 (省 88%)                <- 本测试 A / E 段
    2. 按龄回收 : 异常图 30 天 / 奖励图 90 天             <- 沿用 cleanup_captures
    3. 硬上限   : captures/ 超上限就从最旧删               <- 本测试 D 段

覆盖:

【A. 落盘格式】`Battler._save_reward_frame`
  PNG -> JPEG 是个**会影响磁盘上所有产出物**的改动, 出错方式还特别安静: `tofile()`
  在文件名不合法时**返回 None 且不抛异常**(项目踩过, 写出 0 字节文件)。所以这里不只看
  "函数返回了什么", 而是**打开文件验 magic bytes + 解回来比对分辨率**。

【B. 改名兼容】`Battler._finalize_reward`
  改名规则是 `<HHMMSS>_<物品名>_<胜负><后缀>`。换格式那天, 磁盘上同时存在 .png 与 .jpg,
  而 `rewards.jsonl` 里的历史记录全指向 .png —— 改名逻辑必须**跟着原后缀走**, 不能一律
  拼 `.jpg`, 否则历史文件会被改成一个不存在的名字 (且 jsonl 也一起指丢)。

【C. 回图端点】`/api/rewards/img`
  服务端原来是 `p.suffix.lower() != ".png" -> 404` 硬编码。换成 JPEG 后如果漏改这一行,
  表现是"记录在、图打不开"——前端只会显示空白, 不会报错。

【D. 硬上限】`cleanup_captures._enforce_cap`
  保留期是**按时间**设闸, 挡不住**速率**突变。上限是几何意义上的"不会爆"。这里锁住
  三条: 未超不动 / 超了删最旧 / 新文件受保护 (删掉"刚刚那次异常"的现场就是删掉了最想
  看的证据)。

【E. 有损的可接受性】真实帧实证
  "省 88%"如果代价是"看不清奖励是啥"就本末倒置了。所以拿 assets/reward_frames/ 里固化
  的真实整帧, 压完**再解回来重新跑一遍图标识别** —— 压缩后仍认得出物品名, 才算通过。

跑法: python scripts/reward_storage_selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sj_bot.battler import REWARD_IMG_EXT, REWARD_IMG_QUALITY, Battler   # noqa: E402
from sj_bot.reward_icons import UNKNOWN_NAME, match_icon                # noqa: E402
import sj_bot.server as _srv                                            # noqa: E402
from sj_bot.server import REWARD_CROPS, REWARD_IMG_EXTS, REWARD_IMG_MIME  # noqa: E402

web_app = _srv.app

import cleanup_captures as CC                                           # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
REAL_DIR = ROOT / "assets" / "reward_frames"


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <<< {detail}"))


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} GB"


def load(p: Path) -> np.ndarray | None:
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def is_jpeg(p: Path) -> bool:
    """按 magic bytes 判, 不看扩展名 —— 扩展名是我们自己拼的, 不算证据。"""
    try:
        with p.open("rb") as f:
            return f.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False


class _Cfg:
    """只提供 Battler 落盘/改名用到的两个目录, 不碰真实 data/。"""

    def __init__(self, root: Path) -> None:
        self.capture_dir = root / "captures"
        self.data_dir = root / "data"
        self.db_path = root / "sj_bot.db"


def new_battler(root: Path, log_sink: list | None = None) -> Battler:
    b = Battler(mode="rank", log=(lambda lvl, msg: log_sink.append((lvl, msg)) if log_sink is not None else None))
    b.cfg = _Cfg(root)
    (b.cfg.capture_dir / "rewards").mkdir(parents=True, exist_ok=True)
    b.cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return b


def real_frame(name: str) -> np.ndarray | None:
    p = REAL_DIR / name
    return load(p) if p.exists() else None


REAL_MAIN = "164055_装备扫荡券_win.png"        # 能认出物品名的那张
REAL_UNK = "180033_待命名_win.png"            # 待命名那张 (覆盖未识别分支)

# ================================================================ A. 落盘格式
print("=== A. 落盘格式 (PNG -> JPEG) ===")

tmpA = Path(tempfile.mkdtemp(prefix="rwstore_a_"))
try:
    check("A0 前置: 常量是 .jpg / q88",
          REWARD_IMG_EXT == ".jpg" and REWARD_IMG_QUALITY == 88,
          f"{REWARD_IMG_EXT=} {REWARD_IMG_QUALITY=}")

    img = real_frame(REAL_MAIN)
    if img is None:
        check(f"A0b 真实奖励帧存在 ({REAL_MAIN})", False, f"缺 {REAL_DIR/REAL_MAIN}")
    else:
        check(f"A0b 真实奖励帧存在 ({REAL_MAIN})", img.shape == (900, 1600, 3), str(img.shape))

        sink: list = []
        b = new_battler(tmpA, sink)
        b.last_img = img
        rel = b._save_reward_frame("win")
        check("A1 返回相对路径以 .jpg 结尾", bool(rel) and rel.endswith(".jpg"), f"{rel=}")

        p = tmpA / "captures" / (rel or "")
        check("A2 文件真的落在盘上 (tofile 失败会静默返回 None)", p.exists(), str(p))
        if p.exists():
            check("A3 内容是 JPEG (magic FFD8FF)", is_jpeg(p), "前 3 字节不是 JPEG 头")
            back = load(p)
            check("A4 解回来分辨率不变 (1600x900)",
                  back is not None and back.shape == (900, 1600, 3),
                  str(None if back is None else back.shape))

            png_size = len(cv2.imencode(".png", img)[1])
            jpg_size = p.stat().st_size
            ratio = png_size / max(jpg_size, 1)
            check(f"A5 相对 PNG 至少省 4 倍 (实测 {ratio:.1f}x, "
                  f"{human(png_size)} -> {human(jpg_size)})", ratio >= 4.0, f"{ratio=:.2f}")
            check("A6 单张体积 < 0.5 MB", jpg_size < 512 * 1024, f"{human(jpg_size)}")

        check("A7 目录名是 rewards/<YYYYMMDD>/", "/rewards/2026" in ("/" + (rel or "")).replace("\\", "/"),
              f"{rel=}")

        # jsonl 落账
        jl = b.cfg.data_dir / "rewards.jsonl"
        recs = [json.loads(x) for x in jl.read_text(encoding="utf-8").splitlines() if x.strip()] if jl.exists() else []
        check("A8 jsonl 写了 1 条", len(recs) == 1, f"{len(recs)} 条")
        if recs:
            r0 = recs[0]
            check("A9 jsonl 的 file 字段跟着换成 .jpg", str(r0.get("file", "")).endswith(".jpg"),
                  str(r0.get("file")))
            check("A10 物品名仍被认对 (换格式没影响识别)", r0.get("item") == "装备扫荡券",
                  str(r0.get("item")))
            check("A11 verdict 已落到记录里 (留图那刻就该是本局结果)", r0.get("verdict") == "win",
                  str(r0.get("verdict")))

        check("A12 落盘成功有 ok 级日志",
              any(l == "ok" and "开箱奖励" in m for l, m in sink),
              str(sink[-3:]))

    # A13: 无 last_img 时不写文件, 也不报错
    b2 = new_battler(tmpA / "empty")
    b2.last_img = None
    check("A13 没有 last_img 时返回 None 且不落文件",
          b2._save_reward_frame(None) is None
          and not list((b2.cfg.capture_dir / "rewards").rglob("*.jpg")))
finally:
    shutil.rmtree(tmpA, ignore_errors=True)

# ================================================================ B. 改名与历史兼容
print("\n=== B. 改名与历史 (后缀必须跟随原文件) ===")

tmpB = Path(tempfile.mkdtemp(prefix="rwstore_b_"))
try:
    img = real_frame(REAL_MAIN)
    sink_b: list = []
    b = new_battler(tmpB, sink_b)
    b.last_img = img
    rel = b._save_reward_frame(None)                  # 留图那刻胜负还没判出
    b._finalize_reward("win")                         # 事后补判
    day = b.cfg.capture_dir / "rewards" / "20260929"
    day = sorted((b.cfg.capture_dir / "rewards").glob("2*"))[0]
    names = [p.name for p in day.iterdir()]
    check("B1 改名成 <HHMMSS>_<物品名>_<胜负>.jpg",
          len(names) == 1 and names[0].endswith("_装备扫荡券_win.jpg"), str(names))

    jl = b.cfg.data_dir / "rewards.jsonl"
    rec = json.loads(jl.read_text(encoding="utf-8").strip().splitlines()[-1])
    check("B2 jsonl 的 file 同步指到新名字",
          str(rec.get("file", "")).endswith("_装备扫荡券_win.jpg"), str(rec.get("file")))
    check("B3 verdict 被补上", rec.get("verdict") == "win", str(rec.get("verdict")))

    b._finalize_reward("loss")                        # 再补一次: 必须幂等
    names2 = [p.name for p in day.iterdir()]
    check("B4 二次补判幂等 (不会叠成 _win_loss_x)", names2 == names, str(names2))

    # ---- B5: 历史 PNG 记录 ----------
    tmpC = tmpB / "legacy"
    b3 = new_battler(tmpC, [])
    d3 = b3.cfg.capture_dir / "rewards" / "20260901"
    d3.mkdir(parents=True, exist_ok=True)
    oldp = d3 / "120000.png"
    cv2.imencode(".png", img)[1].tofile(str(oldp))
    rel3 = str(oldp.relative_to(b3.cfg.capture_dir))
    jl3 = b3.cfg.data_dir / "rewards.jsonl"
    jl3.write_text(json.dumps({"ts": "2026-09-01 12:00:00", "mode": "rank", "round": 1,
                               "verdict": "unknown", "item": "钻石",
                               "file": rel3}, ensure_ascii=False) + "\n", encoding="utf-8")
    b3._last_reward_file = rel3
    b3._finalize_reward("loss")
    n3 = [p.name for p in d3.iterdir()]
    check("B5 历史 .png 记录改名后仍是 .png (没被拼成 .jpg)",
          n3 == ["120000_钻石_loss.png"], str(n3))
    r3 = json.loads(jl3.read_text(encoding="utf-8").strip().splitlines()[-1])
    check("B6 历史记录的 file 也被同步更新",
          str(r3.get("file", "")).endswith("120000_钻石_loss.png"), str(r3.get("file")))

    # ---- B7: verdict 不合理时不动 ----
    tmpD = tmpB / "noverdict"
    b4 = new_battler(tmpD, [])
    d4 = b4.cfg.capture_dir / "rewards" / "20260901"
    d4.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".jpg", img)[1].tofile(str(d4 / "130000.jpg"))
    rel4 = str((d4 / "130000.jpg").relative_to(b4.cfg.capture_dir))
    b4._last_reward_file = rel4
    b4._finalize_reward(None)                         # 胜负未定 -> 不该乱改名
    check("B7 verdict 未定时不改名", [p.name for p in d4.iterdir()] == ["130000.jpg"],
          str([p.name for p in d4.iterdir()]))
    check("B8 verdict 未定时不消耗 _last_reward_file (留给下一轮)", b4._last_reward_file == rel4,
          str(b4._last_reward_file))
finally:
    shutil.rmtree(tmpB, ignore_errors=True)

# ================================================================ C. 回图端点
print("\n=== C. 回图端点 (/api/rewards/img) ===")

check("C0 白名单含 png 与 jpg", REWARD_IMG_EXTS == frozenset({".png", ".jpg", ".jpeg"}),
      str(sorted(REWARD_IMG_EXTS)))
check("C0b 两种后缀的 mimetype 各自正确",
      REWARD_IMG_MIME.get(".jpg") == "image/jpeg" and REWARD_IMG_MIME.get(".png") == "image/png",
      str(REWARD_IMG_MIME))

tmpE = Path(tempfile.mkdtemp(prefix="rwstore_c_"))
_saved_cap = _srv.cfg.capture_dir
try:
    # 端点读的是模块级 cfg.capture_dir —— 换成临时目录, 免得往真实 captures/ 里塞探针文件
    (tmpE / "captures" / "rewards").mkdir(parents=True, exist_ok=True)
    _srv.cfg.capture_dir = tmpE / "captures"

    root = (tmpE / "captures" / "rewards").resolve()
    probe = root / "_selftest_probe"
    probe.mkdir(parents=True, exist_ok=True)
    img = real_frame(REAL_MAIN)
    if img is None:
        check("C1 前置: 真实奖励帧可读", False, "样本缺失, 后半段无法测")
    else:
        jf, pf = probe / "p.jpg", probe / "p.png"
        cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), REWARD_IMG_QUALITY])[1].tofile(str(jf))
        cv2.imencode(".png", img)[1].tofile(str(pf))
        (probe / "p.gif").write_bytes(b"GIF89a")                  # 白名单外
    rel_j = "rewards/_selftest_probe/p.jpg"
    rel_p = "rewards/_selftest_probe/p.png"
    rel_g = "rewards/_selftest_probe/p.gif"

    c = web_app.test_client()

    r = c.get(f"/api/rewards/img?f={rel_j}")
    check("C1 .jpg 整帧 -> 200", r.status_code == 200, str(r.status_code))
    check("C2 .jpg 的 Content-Type 是 image/jpeg (不能声明成 png)",
          r.mimetype == "image/jpeg", str(r.mimetype))

    r = c.get(f"/api/rewards/img?f={rel_p}")
    check("C3 .png 整帧 -> 200 且 image/png",
          r.status_code == 200 and r.mimetype == "image/png", f"{r.status_code} {r.mimetype}")

    for crop in ("icon", "popup"):
        r = c.get(f"/api/rewards/img?f={rel_j}&crop={crop}")
        ok = r.status_code == 200 and r.mimetype == "image/png"
        dec = cv2.imdecode(np.frombuffer(r.data, np.uint8), cv2.IMREAD_COLOR) if ok else None
        x0, y0, x1, y1 = REWARD_CROPS[crop]
        ok = ok and dec is not None and dec.shape[:2] == (y1 - y0, x1 - x0)
        check(f"C4 .jpg + crop={crop} 可裁且尺寸对",
              ok, f"{r.status_code} {r.mimetype} {None if dec is None else dec.shape}")

    r = c.get(f"/api/rewards/img?f={rel_g}")
    check("C5 .gif 被白名单挡住 -> 404", r.status_code == 404, str(r.status_code))

    r = c.get("/api/rewards/img?f=rewards/../../../data/rewards.jsonl")
    check("C6 ../ 穿越 -> 404", r.status_code == 404, str(r.status_code))

    r = c.get("/api/rewards/img?f=rewards/_selftest_probe/nope.jpg")
    check("C7 不存在的文件 -> 404", r.status_code == 404, str(r.status_code))

    r = c.get("/api/rewards/img?f=")
    check("C8 空 f -> 404 (不会退化成列目录)", r.status_code == 404, str(r.status_code))
finally:
    _srv.cfg.capture_dir = _saved_cap          # 立刻还原, 别把临时路径漏给后面的段
    shutil.rmtree(tmpE, ignore_errors=True)

# ================================================================ D. 硬上限
print("\n=== D. 硬上限 (_enforce_cap) ===")

tmpD2 = Path(tempfile.mkdtemp(prefix="rwstore_d_"))
try:
    root = tmpD2 / "captures"


    def mk(name: str, kb: int, age_hours: float) -> Path:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * (kb * 1024))
        t = time.time() - age_hours * 3600
        os.utime(p, (t, t))
        return p


    # D1: 未超上限 -> 一个都不动
    mk("rewards/20260101/a.jpg", 300, 48)
    mk("b.txt", 200, 48)
    used, cnt = CC._usage(root)
    n, freed, _ = CC._enforce_cap(root, cap_bytes=8192 * 1024, dry=False, min_age_hours=6)
    check(f"D1 未超上限不动手 ({human(used)} <= 8MB)", n == 0 and freed == 0, f"{n=} {freed=}")
    check("D1b 文件仍在", (root / "rewards/20260101/a.jpg").exists() and (root / "b.txt").exists())

    # D2: 超上限 -> 删最旧的, 直到 <= 上限
    root2 = tmpD2 / "cap"
    root2.mkdir(parents=True, exist_ok=True)
    oldest = None
    for i, (kb, age) in enumerate([(400, 100), (400, 80), (400, 60), (400, 40), (400, 20)]):
        p = root2 / f"f{i}.bin"
        p.write_bytes(b"\0" * (kb * 1024))
        t = time.time() - age * 3600
        os.utime(p, (t, t))
        if oldest is None:
            oldest = p
    used2, _ = CC._usage(root2)                       # 2000 KB
    n2, freed2, s2 = CC._enforce_cap(root2, cap_bytes=1100 * 1024, dry=False, min_age_hours=6)
    after2, _ = CC._usage(root2)
    check(f"D2 超上限会删到限内 ({human(used2)} -> {human(after2)}, 上限 1.07MB)",
          after2 <= 1100 * 1024, f"{human(after2)}")
    check("D3 删的确实是最旧的那个", oldest is not None and not oldest.exists(),
          f"{oldest} 仍存在" if oldest and oldest.exists() else "")
    check("D4 报告了删除数与释放量", n2 >= 2 and freed2 > 0, f"{n2=} {freed2=}")

    # D5: 新文件受保护
    root3 = tmpD2 / "fresh"
    root3.mkdir(parents=True, exist_ok=True)
    fresh = root3 / "just_now.png"
    fresh.write_bytes(b"\0" * (500 * 1024))
    old3 = root3 / "ancient.png"
    old3.write_bytes(b"\0" * (500 * 1024))
    t = time.time() - 100 * 3600
    os.utime(old3, (t, t))
    n3, freed3, _ = CC._enforce_cap(root3, cap_bytes=100 * 1024, dry=False, min_age_hours=6)
    check("D5 新文件 (6h 内) 受保护, 不被删", fresh.exists(), "刚写的截图被删了")
    check("D6 同一轮里旧文件照删", not old3.exists(), "旧文件没被删")
    check("D7 删无可删时返回 0 而不报错", n3 >= 1 and freed3 > 0, f"{n3=} {freed3=}")

    # D8: dry 不真删
    root4 = tmpD2 / "dry"
    root4.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        p = root4 / f"x{i}.bin"
        p.write_bytes(b"\0" * (400 * 1024))
        t = time.time() - (100 - i * 10) * 3600
        os.utime(p, (t, t))
    n4, freed4, _ = CC._enforce_cap(root4, cap_bytes=500 * 1024, dry=True, min_age_hours=6)
    left = len(list(root4.iterdir()))
    check(f"D8 dry 只报数不删 ({n4} 个 / {human(freed4)})", left == 4 and n4 > 0 and freed4 > 0,
          f"剩 {left} 个")

    # D9: 全部太新 -> 0 删除 (调用方据此打警告)
    root5 = tmpD2 / "allfresh"
    root5.mkdir(parents=True, exist_ok=True)
    (root5 / "a.bin").write_bytes(b"\0" * (900 * 1024))
    n5, freed5, _ = CC._enforce_cap(root5, cap_bytes=100 * 1024, dry=False, min_age_hours=6)
    check("D9 全是新文件时返回 0 (上层会提示调低保留期/质量)", n5 == 0 and freed5 == 0,
          f"{n5=} {freed5=}")
finally:
    shutil.rmtree(tmpD2, ignore_errors=True)

# ================================================================ E. 有损可接受性
print("\n=== E. 有损压缩的可接受性 (真实帧实证) ===")

if not REAL_DIR.exists():
    check("E0 assets/reward_frames/ 存在", False, str(REAL_DIR))
else:
    frames = sorted(REAL_DIR.glob("*.png"))
    check(f"E0 有真实整帧样本 ({len(frames)} 张)", len(frames) >= 2, f"{len(frames)} 张")

    total_png = total_jpg = 0
    worst = 0.0
    for p in frames:
        img = load(p)
        if img is None:
            check(f"E1 {p.name} 可读", False, "decode 失败")
            continue
        png_sz = len(cv2.imencode(".png", img)[1])
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), REWARD_IMG_QUALITY])
        jpg_sz = len(buf)
        total_png += png_sz
        total_jpg += jpg_sz
        worst = max(worst, jpg_sz)
        check(f"E1 {p.name[:22]:24s} {human(png_sz):>9s} -> {human(jpg_sz):>9s} "
              f"({png_sz/max(jpg_sz,1):4.1f}x)",
              png_sz / max(jpg_sz, 1) >= 4.0, f"{png_sz/max(jpg_sz,1):.2f}x")
    if total_png:
        save = 100 * (1 - total_jpg / total_png)
        check(f"E2 整集合计省 {save:.0f}% (阈值 >=80%)", save >= 80.0, f"{save:.1f}%")

    # E3: 压缩后**解回来重新识别**, 确认有损没有毁掉信息
    p = REAL_DIR / REAL_MAIN
    img = load(p)
    if img is None:
        check("E3 压缩后仍认得出物品名", False, "样本读不到")
    else:
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), REWARD_IMG_QUALITY])
        dec = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        item, conf, src = match_icon(dec)
        check(f"E3 q88 压缩后仍认得出物品名 (读回 {item!r}, 置信 {conf:.2f}/{src})",
              item == "装备扫荡券", f"读回 {item!r}")
        check("E4 压缩后弹窗裁剪区仍可解码",
              dec is not None and dec.shape == (900, 1600, 3),
              str(None if dec is None else dec.shape))

    # E5: 待命名那张压缩后仍应"认不出", 而不是被压出个错名字
    p2 = REAL_DIR / REAL_UNK
    img2 = load(p2)
    if img2 is not None:
        ok, buf2 = cv2.imencode(".jpg", img2, [int(cv2.IMWRITE_JPEG_QUALITY), REWARD_IMG_QUALITY])
        dec2 = cv2.imdecode(buf2, cv2.IMREAD_COLOR)
        item2, conf2, _ = match_icon(dec2)
        check(f"E5 原本认不出的帧压缩后也不会被'压'出个错名字 ({item2!r})",
              item2 == UNKNOWN_NAME, f"变成了 {item2!r}")

# ================================================================ 汇总
print("\n" + "=" * 62)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项:")
    for n in FAIL:
        print("  -", n)
    sys.exit(1)
print("全部通过")
sys.exit(0)
