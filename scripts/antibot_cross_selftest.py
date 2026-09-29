# -*- coding: utf-8 -*-
"""防挂机"本地+模型交叉作答"决策自测 (2026-09-10):
mock LLM 与底层检测, 验证 _solve_antibot 决策逻辑:
  1) 本地与模型一致 -> 双重确认, 点模型按钮;  2) 分歧 -> 信模型;
  3) 模型故障 -> 回退本地;  4) 双无 -> 重试至 giveup."""
import sys, os, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 在导入真实模块前注入 fake llm_client ----
calls = {"n": 0, "fail": False, "answer": 2}
fake_llm = types.ModuleType("sj_bot.llm_client")
class _FakeLLM:
    def vision_antibot(self, b64, n_options=4):
        calls["n"] += 1
        if calls["fail"]:
            raise RuntimeError("mock 故障")
        return calls["answer"]
fake_llm.LLMClient = _FakeLLM
fake_llm.encode_cv_image_for_llm = lambda crop, max_side=800: "fake_b64"
fake_llm.get_budget = lambda: {"calls": calls["n"], "cap": 20}
sys.modules["sj_bot.llm_client"] = fake_llm

import numpy as np
from sj_bot.battler import Battler
from sj_bot import rank_layout as rk

x0, y0, _, _ = rk.ANTIBOT_ROI

def make_battler(flat_items, main_city=False, popup_after=True):
    b = object.__new__(Battler)
    img = np.zeros((900, 1600, 3), np.uint8)
    b._shot_img = lambda: img
    b._antibot_ocr_items = lambda im=None: flat_items
    b._sleep_stop = lambda sec: None
    b._save_anomaly = lambda tag: None
    b._save_stage = lambda tag: None
    b._check_antibot = lambda im: popup_after      # 复查: True=弹窗仍在
    b._is_in_main_city = lambda im: main_city
    taps = []
    b._tap = lambda x, y: taps.append((x, y))
    b.taps = taps
    b.log = lambda *a, **k: None
    return b

def rows_for(question="防挂机问题，请在下方选择答案：6+8=?", btns=("5", "14", "8", "9")):
    """_antibot_ocr_items 同构: 扁平 dict 列表, ROI 内坐标"""
    items = [{"text": question, "cx": 600, "cy": 100}]
    items += [{"text": t, "cx": 200 + i * 200, "cy": 259} for i, t in enumerate(btns)]
    return items

def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    return cond

results = []

# ---- case 1: 本地(6+8=14=按钮2) 与模型(answer=2) 一致 -> 点 (701,459), solved ----
calls.update(n=0, fail=False, answer=2)
b = make_battler(rows_for(), main_city=False, popup_after=False)
r = b._solve_antibot()
results.append(check("一致: solved", r == "solved"))
results.append(check("一致: 点按钮2 全图(701,459)", b.taps and b.taps[-1] == (x0 + 401, y0 + 259)))

# ---- case 2: 分歧 (模型 answer=1 即 5, 本地算出14) -> 信模型点按钮1 ----
calls.update(n=0, fail=False, answer=1)
b = make_battler(rows_for(), main_city=False, popup_after=False)
r = b._solve_antibot()
results.append(check("分歧: solved", r == "solved"))
results.append(check("分歧: 信模型点按钮1 (500,459)", b.taps and b.taps[-1] == (x0 + 200, y0 + 259)))

# ---- case 3: 模型故障 -> 回退本地按钮2 (本地 cx 按 mock 数据 = 400, 非实测 401) ----
calls.update(n=0, fail=True, answer=2)
b = make_battler(rows_for(), main_city=False, popup_after=False)
r = b._solve_antibot()
results.append(check("模型故障: solved", r == "solved"))
results.append(check("模型故障: 回退本地点按钮2 (700,459)", b.taps and b.taps[-1] == (x0 + 400, y0 + 259)))

# ---- case 4: 无题干算式 + 模型返回0 -> 重试耗尽 giveup ----
calls.update(n=0, fail=False, answer=0)
b = make_battler(rows_for(question="请回答以下哪个是正确的"), main_city=False, popup_after=False)
r = b._solve_antibot()
results.append(check("双无: giveup (重试耗尽)", r == "giveup"))
results.append(check("双无: 未误点任何按钮", len(b.taps) == 0))

# ---- case 5: 答对但被踢 -> kicked ----
calls.update(n=0, fail=False, answer=2)
b = make_battler(rows_for(), main_city=True, popup_after=False)
r = b._solve_antibot()
results.append(check("答对但落主城: kicked", r == "kicked"))

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
