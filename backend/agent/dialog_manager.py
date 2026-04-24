"""对话管理器 - 打断/宽容期/无响应处理"""
import logging
import asyncio
import time
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BargeInConfig:
    """打断配置"""
    enabled: bool = True
    protect_start_sec: float = 1.0  # 开始播放后 N 秒不响应打断
    protect_end_sec: float = 1.0    # 结束前 N 秒不响应打断


@dataclass
class ToleranceConfig:
    """宽容期配置"""
    enabled: bool = True
    duration_ms: int = 500  # 宽容期时长（从1000ms降至500ms，降低等待延迟）


@dataclass
class NoResponseConfig:
    """无响应配置"""
    timeout_sec: int = 5
    mode: str = "consecutive"  # consecutive | cumulative
    max_count: int = 3
    prompt: str = "您好，请问您还在吗？"
    hangup_text: str = "感谢您的时间，再见！"


class DialogManager:
    """对话管理器

    负责管理一次通话中的打断检测、宽容期处理和无响应追踪。
    从话术配置初始化，贯穿整个通话生命周期。
    """

    def __init__(self, script_config: dict):
        """从话术配置初始化"""
        sc = script_config or {}

        # 打断配置
        self.barge_in = BargeInConfig(
            enabled=sc.get("barge_in_conversation", True),
            protect_start_sec=sc.get("barge_in_protect_start_sec", 1.0),
            protect_end_sec=sc.get("barge_in_protect_end_sec", 1.0),
        )

        # 宽容期配置
        self.tolerance = ToleranceConfig(
            enabled=sc.get("tolerance_enabled", True),
            duration_ms=sc.get("tolerance_ms", 1000),
        )

        # 无响应配置
        self.no_response = NoResponseConfig(
            timeout_sec=sc.get("no_response_timeout_sec", 5),
            mode=sc.get("no_response_mode", "consecutive"),
            max_count=sc.get("no_response_max_count", 3),
            prompt=sc.get("no_response_prompt", "您好，请问您还在吗？"),
            hangup_text=sc.get("no_response_hangup_text", "感谢您的时间，再见！"),
        )

        # 运行时状态
        self._speaking_start_time: Optional[float] = None
        self._speaking_duration: Optional[float] = None
        self._no_response_consecutive_count: int = 0
        self._no_response_cumulative_count: int = 0
        self._total_user_talk_time: float = 0
        self._total_ai_talk_time: float = 0
        self._rounds: int = 0

    # ---- 打断相关 ----

    def on_ai_speaking_start(self, estimated_duration: float = 0):
        """AI 开始播报时调用"""
        self._speaking_start_time = time.time()
        self._speaking_duration = estimated_duration

    def on_ai_speaking_end(self):
        """AI 播报结束时调用"""
        elapsed = time.time() - (self._speaking_start_time or time.time())
        self._total_ai_talk_time += elapsed
        self._speaking_start_time = None

    def should_allow_barge_in(self) -> bool:
        """判断当前是否允许打断

        保护期逻辑：
        - 播放开始后 protect_start_sec 内不允许打断
        - 如果已知总时长，结束前 protect_end_sec 内不允许打断
        """
        if not self.barge_in.enabled:
            return False

        if self._speaking_start_time is None:
            return False

        elapsed = time.time() - self._speaking_start_time

        # 开始保护期
        if elapsed < self.barge_in.protect_start_sec:
            return False

        # 结束保护期
        if self._speaking_duration and self._speaking_duration > 0:
            remaining = self._speaking_duration - elapsed
            if remaining < self.barge_in.protect_end_sec:
                return False

        return True

    # ---- 宽容期相关 ----

    async def wait_with_tolerance(
        self,
        initial_text: str,
        listen_func,  # async callable 返回 Optional[str]
    ) -> str:
        """宽容期等待

        用户说完第一句后，等待 tolerance_ms，期间继续监听。
        如果有新输入，合并后返回。

        Args:
            initial_text: 用户首次输入文本
            listen_func: 监听函数，返回新输入文本或 None

        Returns:
            合并后的完整用户输入
        """
        if not self.tolerance.enabled or self.tolerance.duration_ms <= 0:
            return initial_text

        combined = initial_text
        try:
            additional = await asyncio.wait_for(
                listen_func(),
                timeout=self.tolerance.duration_ms / 1000.0,
            )
            if additional:
                combined = f"{initial_text} {additional}"
                logger.info(f"宽容期内收到补充: {additional}")
        except asyncio.TimeoutError:
            pass  # 宽容期结束，无新输入

        return combined

    # ---- 无响应相关 ----

    def on_user_responded(self, text: str, duration_sec: float = 0):
        """用户有响应时调用"""
        self._rounds += 1
        self._total_user_talk_time += duration_sec
        if self.no_response.mode == "consecutive":
            self._no_response_consecutive_count = 0

    def on_no_response(self) -> dict:
        """用户无响应时调用

        Returns:
            {
                "should_hangup": bool,
                "prompt_text": str,  # 追问文本或挂断语
                "consecutive_count": int,
                "cumulative_count": int,
            }
        """
        self._no_response_consecutive_count += 1
        self._no_response_cumulative_count += 1

        # 判断是否达到上限
        if self.no_response.mode == "consecutive":
            should_hangup = self._no_response_consecutive_count >= self.no_response.max_count
        else:
            should_hangup = self._no_response_cumulative_count >= self.no_response.max_count

        return {
            "should_hangup": should_hangup,
            "prompt_text": self.no_response.hangup_text if should_hangup else self.no_response.prompt,
            "consecutive_count": self._no_response_consecutive_count,
            "cumulative_count": self._no_response_cumulative_count,
        }

    # ---- 统计 ----

    def get_stats(self) -> dict:
        """获取通话统计"""
        return {
            "rounds": self._rounds,
            "user_talk_time_sec": round(self._total_user_talk_time, 2),
            "ai_talk_time_sec": round(self._total_ai_talk_time, 2),
            "no_response_consecutive": self._no_response_consecutive_count,
            "no_response_cumulative": self._no_response_cumulative_count,
        }
