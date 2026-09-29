"""防挂机弹窗本地求解 (零成本优先, 模型兜底)。

原则 (2026-09-09 用户定): 大模型只在"基本方法做不了"时才调用。
实测弹窗为纯算术选择题 (题干 "防挂机问题，请在下方选择答案：2+8=?" +
底部 4 个数字按钮 4/16/13/10), OCR 对白字数字相当可靠, 完全可以用
本地解析命中按钮, 不需要把截图发给视觉模型 (视觉模型贵且慢)。

对外接口:
  math_answer(text)          -> 从题干文本解算式, 返回结果 int 或 None
  solve_math_option(items)   -> 给定 OCR items, 返回 (按钮在弹窗 ROI 内的
                                cx, cy) 或 None (本地解不了, 调用方才走模型)

历史触发样本 (2026-09-08 实测, 6 张截图跨 1 小时一致):
  题干: "防挂机问题，请在下方选择答案：2+8=?"  (正确答案 10 -> 第 4 个按钮)
  按钮行 OCR 全图 y=459 -> ROI 内 cy=259 (见 rank_layout.ANTIBOT_OPTION_COORDS)
"""
from __future__ import annotations

import re
from typing import Optional

# 按钮数字行在弹窗 ROI 内的 y 带 (实测 cy≈259, 给 ±70px 容错; 题干/杂项文本不在此带)
_OPTION_BAND = (180, 340)
# 参与解析的题干提示 (防止把普通文本里的 "a+b" 当算式误伤)
_QUESTION_MARKERS = ("=", "？", "?", "＝", "答案", "于", "选", "几", "多少")

_OPS = {
    "+": lambda a, b: a + b,
    "＋": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "－": lambda a, b: a - b,
    "x": lambda a, b: a * b,
    "X": lambda a, b: a * b,
    "*": lambda a, b: a * b,
    "×": lambda a, b: a * b,
}


def math_answer(line: str) -> Optional[int]:
    """从单行/整窗文本里识别唯一算式, 返回结果 int; 识别不到/多个不一致 -> None。

    只接受形如 "2+8=?" / "6x7=?" / "12-4" 且行内含题目提示词的情况,
    避免把随机数字串误判成算式。除号(/)暂不解析 (弹出题实测只有 +-×)。"""
    if not line or not any(m in line for m in _QUESTION_MARKERS):
        return None
    pat = re.compile(r"(\d{1,3})\s*([+\-xX*×＋－])\s*(\d{1,3})\s*[=＝]?\s*[?？]?")
    found: Optional[int] = None
    for m in pat.finditer(line):
        a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
        fn = _OPS.get(op)
        if fn is None:
            continue
        val = fn(a, b)
        if found is None:
            found = val
        elif found != val:
            return None  # 出现多个不一致算式 -> 不可靠, 交模型
    return found


def solve_math_option(items: list[dict]) -> Optional[tuple[int, int]]:
    """在 OCR items 里本地解算术题并定位按钮。

    items 需含 text/cx/cy 且坐标已是**弹窗 ROI 内坐标** (调用方裁剪后加过偏移
    或本身就在裁剪图内测的). 按钮候选 = 按钮带内的纯整数文字块.
    返回 (按钮 ROI 内 cx, cy); 任一环节不确定 -> None (调用方再走视觉模型).

    注意: **逐条文本项解析**, 不要拼接全文——弹窗背景上常混有赛季日期
    ("08-23" 会被误当 8-23)、区服号("40区")等数字文本, 拼接会制造"多式不一致"
    假象导致本地解法失效 (2026-09-09 实测 6 张弹窗全部 None 的根因)."""
    if not items:
        return None
    # 逐项识别算式; 多项结果须全部一致才可信 (双 OCR 路径同一题干会出两次)
    answers: list[int] = []
    for it in items:
        v = math_answer(it.get("text") or "")
        if v is not None:
            answers.append(v)
    if not answers:
        return None
    first = answers[0]
    if any(v != first for v in answers):
        return None
    want = str(first)
    # 候选按钮 = 纯整数 + 落在按钮行 y 带
    for it in items:
        t = (it.get("text") or "").strip()
        cy = it.get("cy", 0)
        if t == want and len(t) <= 4 and _OPTION_BAND[0] <= cy <= _OPTION_BAND[1]:
            return (int(it.get("cx", 0)), int(cy))
    return None


if __name__ == "__main__":  # 纯逻辑自测
    def check(name: str, cond: bool) -> None:
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            raise SystemExit(1)

    print("== math_answer ==")
    check("2+8=?", math_answer("防挂机问题，请在下方选择答案：2+8=?") == 10)
    check("6x7=?", math_answer("6x7=?") == 42)
    check("12-4", math_answer("请计算 12-4 等于几") == 8)
    check("无算式/无提示词 -> None", math_answer("今天星期几") is None)
    check("两式不一致 -> None", math_answer("1+1=2 且 2+2=?") is None)

    print("== solve_math_option (合成 OCR 行, ROI 内坐标) ==")
    mk = lambda t, cx, cy: {"text": t, "cx": cx, "cy": cy}
    items = [
        mk("防挂机问题，请在下方选择答案：2+8=?", 600, 100),
        mk("4", 200, 259), mk("16", 401, 259), mk("13", 600, 259), mk("10", 800, 259),
    ]
    r = solve_math_option(items)
    check("命中答案按钮10 @ (800,259)", r == (800, 259))

    items2 = [mk("防挂机问题，请在下方选择答案：2+8=?", 600, 100), mk("4", 200, 259)]
    check("答案不在候选 -> None (交模型)", solve_math_option(items2) is None)
    check("题干数字 8 在按钮带外不误选", solve_math_option([
        mk("8", 620, 100), mk("防挂机问题，请在下方选择答案：2+8=?", 600, 100),
        mk("4", 200, 259), mk("10", 800, 259)]) == (800, 259))
    # 回归 (2026-09-09 真实弹窗全 None 根因): 背景混有日期/区服号, 必须逐项解析
    check("真实弹窗场景: 混入日期'08-23'与'40区'等仍命中", solve_math_option([
        mk("40区", 940, 41), mk("08-23至202", 58, 64), mk("42区", 940, 88),
        mk("余时间：11天", 56, 94), mk("防挂机问题，请在下方选择答案：2+8=?", 501, 184),
        mk("16", 401, 259), mk("13", 600, 259), mk("10", 800, 259)]) == (800, 259))
    check("两个不同算式并存 -> None", solve_math_option([
        mk("1+1=?", 300, 100), mk("2+2=?", 600, 100),
        mk("2", 200, 259), mk("4", 800, 259)]) is None)
    print("antibot_solver 自测通过")
