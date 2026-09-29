"""DeepSeek 文本/视觉问答客户端 (防挂机选择题, **仅当本地方法解不了才调用**)。

接口 OpenAI 兼容: POST {base}/chat/completions
配置 (环境变量优先, 其次走 config.llm):
  SJ_LLM_KEY    API key
  SJ_LLM_BASE   base url (默认 https://api.deepseek.com)
  SJ_LLM_MODEL  模型名 (默认 deepseek-chat)
  SJ_LLM_DAILY_CALLS  每日付费调用上限 (默认 20, 防预算被吃穿)

省钱约定 (2026-09-09 用户要求"只在必要时调模型"):
  1. 调用前先过每日预算闸门 (data/llm_usage.json 持久化, 按自然日清零),
     超限直接 LLMError 拒绝, 绝不静默超支;
  2. 只有 HTTP 200 才算一次"成功调用"并计数 (402/429/网络错误不扣钱, 允许重试);
  3. 已拿到 200 响应但内容解析失败 -> **不再付费重试** (上一条已扣钱, 重试=重复扣),
     记日志返回失败由上层决定 (可换本地方法/放弃), 只有非 200/网络异常才重试;
  4. 发送给视觉模型的图先压缩 (encode_cv_image_for_llm, 长边 ≤800), 降 token 消耗.

对外接口: ask_text() / choose_option() / pick_index() / choose_option_loose() /
          vision_antibot() / encode_cv_image_for_llm() / get_budget().
调用失败抛 LLMError, 由调用方决定降级策略 (绝不静默吞掉导致乱点).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("llm_client")

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
TIMEOUT_SEC = 30
MAX_RETRY = 2            # 仅用于非 200 / 网络层重试 (不产生新扣费)
_DAILY_CAP_DEFAULT = 20  # 每日成功(付费)调用上限; SJ_LLM_DAILY_CALLS 可覆盖

# ---------------------------------------------------------------- 预算台账
_usage_path: Optional[Path] = None
_usage_lock = threading.Lock()


def _usage_file() -> Path:
    global _usage_path
    if _usage_path is None:
        try:
            from sj_bot.config import Config
            _usage_path = Config().data_dir / "llm_usage.json"
        except Exception:
            _usage_path = Path("data/llm_usage.json")
    try:
        _usage_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return _usage_path


def _load_usage() -> dict:
    try:
        with open(_usage_file(), encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        return d
    except Exception:
        return {}


def daily_cap() -> int:
    try:
        return max(1, int(os.environ.get("SJ_LLM_DAILY_CALLS") or _DAILY_CAP_DEFAULT))
    except ValueError:
        return _DAILY_CAP_DEFAULT


def get_budget() -> dict:
    """今日模型用量快照: {date, calls, cap, bytes_sent} (供状态接口/调试显示)."""
    with _usage_lock:
        d = _load_usage()
    today = time.strftime("%Y-%m-%d")
    if d.get("date") != today:
        return {"date": today, "calls": 0, "cap": daily_cap(), "bytes_sent": 0}
    return {
        "date": today, "calls": int(d.get("calls", 0)),
        "cap": daily_cap(), "bytes_sent": int(d.get("bytes", 0)),
    }


def _check_budget() -> None:
    """调用前闸门: 今日成功次数已到上限 -> 抛错 (防吃穿预算)."""
    cap = daily_cap()
    if get_budget()["calls"] >= cap:
        raise LLMError(
            f"今日模型调用已达上限 {cap} 次 (本地方法解不了才会走到这), "
            "已停止付费调用, 请明日再试或设 SJ_LLM_DAILY_CALLS 提高上限")


def _tick_usage(bytes_sent: int = 0) -> int:
    """记账一次成功(付费)调用, 返回今日累计次数. 线程安全, 落盘防崩溃丢账."""
    with _usage_lock:
        d = _load_usage()
        today = time.strftime("%Y-%m-%d")
        if d.get("date") != today:
            d = {"date": today, "calls": 0, "bytes": 0}
        d["calls"] = int(d.get("calls", 0)) + 1
        d["bytes"] = int(d.get("bytes", 0)) + int(bytes_sent)
        try:
            with open(_usage_file(), "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except Exception:
            pass
        return int(d["calls"])


def encode_cv_image_for_llm(cv_img, max_side: int = 800) -> str:
    """把 OpenCV BGR 图像等比压缩到长边 ≤ max_side 后 base64.

    视觉模型按分辨率计 token, 原样发 1000x500 弹窗图很贵; 按钮/题干字大,
    800px 长边足够识别 (实测按钮数字是白字大号). 返回 data URL 用 base64 串."""
    import base64
    import cv2
    h, w = cv_img.shape[:2]
    scale = min(1.0, max_side / float(max(h, w)))
    if scale < 1.0:
        cv_img = cv2.resize(cv_img, (max(1, int(w * scale)), max(1, int(h * scale))),
                            interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", cv_img)
    if not ok:
        raise LLMError("图片编码失败")
    return base64.b64encode(buf.tobytes()).decode()


class LLMError(RuntimeError):
    pass


def _env_or(key: str, default: str) -> str:
    return os.environ.get(key) or default


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = TIMEOUT_SEC,
    ) -> None:
        # 未显式传入时: 环境变量 > 项目 Config 默认值
        if api_key is None or base is None or model is None:
            try:
                from sj_bot.config import Config
                cfg = Config()
            except Exception:
                cfg = None
            cfg_key = getattr(cfg, "llm_key", "") if cfg else ""
            cfg_base = getattr(cfg, "llm_base", DEFAULT_BASE) if cfg else DEFAULT_BASE
            cfg_model = getattr(cfg, "llm_model", DEFAULT_MODEL) if cfg else DEFAULT_MODEL
            api_key = api_key or _env_or("SJ_LLM_KEY", cfg_key)
            base = base or _env_or("SJ_LLM_BASE", cfg_base)
            model = model or _env_or("SJ_LLM_MODEL", cfg_model)
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        if not self.api_key:
            raise LLMError("未配置 SJ_LLM_KEY (DeepSeek API key)")
        # 视觉模型名: 显式可配 (SJ_LLM_VISION_MODEL > config.llm_vision_model > 常量).
        # 官方 api.deepseek.com 没有视觉模型 —— 需要走第三方网关时务必配齐 SJ_LLM_BASE +
        # SJ_LLM_VISION_MODEL, 避免默认别名打到错误的服务商产生意外账单.
        try:
            from sj_bot.config import Config as _Cfg
            vm = _Cfg().llm_vision_model
        except Exception:
            vm = _env_or("SJ_LLM_VISION_MODEL", "deepseek-v4-flash-vision-exp")
        self.vision_model = _env_or("SJ_LLM_VISION_MODEL", vm)

    # ------------------------------------------------------------ 底层
    def _chat(self, messages: list[dict], temperature: float = 0.0) -> str:
        """单次 chat 请求。预算闸门先行; 仅网络层/非 200 重试 (重试不新增扣费)."""
        _check_budget()
        url = f"{self.base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 200,
            "stream": False,
        }
        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRY + 1):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if r.status_code != 200:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                n = _tick_usage()
                log.warning("LLM 付费调用成功 #%d (model=%s, 今日 %d/%d)",
                            n, self.model, n, daily_cap())
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
            except LLMError:
                raise
            except Exception as e:  # 网络抖动 -> 重试
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"请求失败: {last_err}")

    # ------------------------------------------------------------ 问答
    def ask_text(self, prompt: str) -> str:
        return self._chat([{"role": "user", "content": prompt}])

    def choose_option(self, question: str, options: list[str]) -> str:
        """把题干+选项给模型, 返回模型判定应选的**选项原文**(用于 OCR 坐标回点)。
        选项去重保序; 只取前 6 个避免超长."""
        opts = list(dict.fromkeys(options))[:6]
        lines = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts))
        prompt = (
            "你在帮我通过一个游戏的防挂机验证(文字选择题)。只根据题干判断唯一正确答案。\n\n"
            f"【题干】\n{question}\n\n"
            f"【选项】\n{lines}\n\n"
            "请只输出正确答案的选项编号(一个整数, 如 2), 不要输出任何其它内容。"
            "如果无法判断, 输出 0。"
        )
        resp = self.ask_text(prompt)
        try:
            idx = int("".join(ch for ch in resp if ch.isdigit())[:2]) - 1
        except (ValueError, IndexError):
            return ""
        if 0 <= idx < len(opts):
            return opts[idx]
        return ""

    def pick_index(self, screen_text: str, candidates: list[str]) -> int:
        """把整窗 OCR 文本 + 候选块(按钮文字) 给模型, 返回应点击的**候选序号**(1-based).
        返回 0 = 模型无法判断; 返回 -1 = 调用异常 (调用方决定重试/放弃).
        比 choose_option 更稳: 候选全部列出, 模型只回序号, 不受题干/选项几何切分影响."""
        cand = list(dict.fromkeys(candidates))[:16]
        if not cand:
            return 0
        lines = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(cand))
        prompt = (
            "你在帮我通过一个游戏的防挂机验证弹窗。屏幕上有题干文字和几个候选按钮文字。\n"
            "请阅读题干, 判断哪个候选按钮是正确的答案, 输出它的编号(一个整数)。\n\n"
            f"【屏幕文字(题干等, 仅供参考)】\n{screen_text[:600]}\n\n"
            f"【候选按钮】\n{lines}\n\n"
            "只输出一个整数编号 (如 2)。无法判断输出 0。不要输出任何其它内容。"
        )
        try:
            resp = self.ask_text(prompt)
            digits = "".join(ch for ch in resp if ch.isdigit())[:3]
            idx = int(digits) if digits else 0
            if idx < 0 or idx > len(cand):
                return 0
            return idx
        except Exception:
            return -1

    # ------------------------------------------------------------ 视觉模型
    def vision_antibot(self, image_b64: str, n_options: int = 4, model: Optional[str] = None,
                       timeout: float = 30.0) -> int:
        """把弹窗截图 base64 发给视觉模型, 询问应点第几个按钮 (1..n_options).
        返回 1-based 序号; 0=模型无法判断; -1=调用异常.
        防挂机弹窗特点: 算术/文字题 + 4 个数字按钮; 视觉模型一图胜千言.
        model 默认取 self.vision_model (SJ_LLM_VISION_MODEL > config)."""
        url = f"{self.base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        model = model or self.vision_model
        prompt = (
            f"图中有游戏的防挂机/验证弹窗(中央带渐变背景的对话框, 可能叠加匹配时的光球特效). "
            f"请忽略边缘光效, 聚焦中央对话框:\n"
            f"1. 阅读题干(弹窗中上部白字, 可能含'请选择答案'等引导语)\n"
            f"2. 数清选项按钮的个数(底部一排按钮, 每个按钮中央写有数字或文字)\n"
            f"3. 计算或判断正确答案, 告诉我应点第几个按钮(从左到右 1..{n_options})\n\n"
            "请按以下 JSON 格式输出: {\"question\":\"题干原文\", \"n_options\":实际按钮数, \"answer_index\":1..n_options}。"
            "无法判断时 answer_index 填 0。"
        )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ]}],
            "max_tokens": 200,
            "temperature": 0.0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        # 省钱规则: 仅"非 200 / 网络异常"重试 (不新增扣费);
        # 已 200 但内容解析失败 = 已经扣过钱 -> 不再重复付费重试, 直接返回失败由上层降级
        _check_budget()
        last_err: Optional[str] = None
        for v_retry in range(MAX_RETRY + 1):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(1.5 * (v_retry + 1))
                    continue  # 非 200 (402/429/5xx) 不扣费, 可重试
                # 200 = 已扣费, 记账一次
                n = _tick_usage(len(image_b64))
                log.warning("LLM 视觉调用成功 #%d (model=%s, 图 %dKB, 今日 %d/%d)",
                            n, model, len(image_b64) // 1024, n, daily_cap())
                data = r.json()
                content = (data["choices"][0]["message"].get("content") or "").strip()
                if not content:
                    last_err = "empty content (200 已扣费, 不再重试)"
                    log.error("vision_antibot 200 但内容为空: %s", last_err)
                    return -1
                # 剥掉模型偶尔输出的 ```json ... ``` 代码围栏, 只留 JSON 主体
                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
                try:
                    j = json.loads(content)
                except Exception as e:
                    # 尝试抢救: 截取首个 { 到末个 } 的 JSON 主体再解析一次
                    a, b = content.find("{"), content.rfind("}")
                    j = None
                    if 0 <= a < b:
                        try:
                            j = json.loads(content[a:b + 1])
                        except Exception:
                            j = None
                    if j is None:
                        last_err = f"JSON parse err: {e} content={content[:80]!r} (200 已扣费, 不再重试)"
                        log.error("vision_antibot %s", last_err)
                        return -1
                idx = int(j.get("answer_index", 0))
                if idx < 0 or idx > n_options:
                    last_err = f"answer_index out of range: {idx} (n_options={n_options}) (200 已扣费, 不再重试)"
                    log.error("vision_antibot %s", last_err)
                    return -1
                return idx
            except Exception as e:
                last_err = f"req err: {e}"
                time.sleep(1.5 * (v_retry + 1))
        # 重试用尽仍未拿到有效结果
        log.error("vision_antibot 重试用尽仍未拿到有效结果, last_err=%s", last_err)
        return -1

    def choose_option_loose(self, question: str, options: list[str]) -> str:
        """宽松版: 模型直接返回选项原文, 用 difflib 在 OCR 选项里做包含匹配兜底。"""
        opts = list(dict.fromkeys(options))[:6]
        lines = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts))
        prompt = (
            "你在帮我通过一个游戏的防挂机验证(文字选择题)。只根据题干判断唯一正确答案。\n\n"
            f"【题干】\n{question}\n\n"
            f"【选项】\n{lines}\n\n"
            "请只输出正确答案的**选项文字本身**(不要编号, 不要解释)。若无法判断输出\"不确定\"。"
        )
        resp = self.ask_text(prompt).strip()
        if not resp or resp == "不确定":
            return ""
        for o in opts:  # 优先精确/包含匹配模型输出里的某个选项
            if o in resp or resp in o:
                return o
        return resp  # 兜底: 原样返回让调用方做模糊匹配
