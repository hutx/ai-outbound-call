"""
对话状态机
管理整个通话的生命周期和意图路由
"""
import logging
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable
from datetime import datetime

logger = logging.getLogger(__name__)


class CallState(Enum):
    """通话状态枚举"""
    DIALING        = auto()  # 拨号中
    RINGING        = auto()  # 振铃中
    CONNECTED      = auto()  # 已接通
    AI_SPEAKING    = auto()  # AI 正在播放语音
    USER_SPEAKING  = auto()  # 用户正在说话
    PROCESSING     = auto()  # LLM 推理中
    TRANSFERRING   = auto()  # 转人工中
    ENDING         = auto()  # 正在结束
    ENDED          = auto()  # 已结束


class CallIntent(Enum):
    """用户意图枚举（由 LLM 判断）"""
    INTERESTED      = "interested"       # 有意向，继续沟通
    NEED_MORE_INFO  = "need_more_info"   # 需要更多信息
    NOT_INTERESTED  = "not_interested"   # 明确拒绝
    BUSY            = "busy"             # 现在不方便
    REQUEST_HUMAN   = "request_human"    # 要求转人工
    CALLBACK        = "callback"         # 要求回拨
    UNKNOWN         = "unknown"          # 未识别


class CallResult(Enum):
    """通话结果枚举"""
    NOT_ANSWERED    = "not_answered"     # 无人接听
    BUSY            = "busy"             # 忙音
    REJECTED        = "rejected"         # 主动拒绝
    COMPLETED       = "completed"        # 正常完成
    TRANSFERRED     = "transferred"      # 已转人工
    ERROR           = "error"            # 异常结束
    BLACKLISTED     = "blacklisted"      # 黑名单


@dataclass
class CallContext:
    """
    单次通话的完整上下文
    贯穿整个通话生命周期，最终写入数据库
    """
    uuid: str                           # FreeSWITCH 通话 UUID
    task_id: str                        # 外呼任务 ID
    phone_number: str                   # 被叫号码
    script_id: str                      # 话术模板 ID
    customer_info: dict = field(default_factory=dict)  # 客户信息（从 CRM 获取）

    # 状态
    state: CallState = CallState.DIALING
    intent: CallIntent = CallIntent.UNKNOWN
    result: CallResult = CallResult.NOT_ANSWERED

    # 时间戳
    created_at: datetime = field(default_factory=datetime.now)
    answered_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    # 对话历史（发送给 LLM 的消息列表）
    messages: list = field(default_factory=list)

    # 统计
    user_utterances: int = 0   # 用户说话次数
    ai_utterances: int = 0     # AI 说话次数
    dtmf_digits: str = ""      # 用户按键

    # 录音文件路径
    recording_path: Optional[str] = None

    # 不满意/拒绝计数（连续拒绝超过阈值则结束）
    rejection_count: int = 0

    # 外呼结果详情（SIP 层面的原始反馈）
    sip_code: Optional[int] = None          # SIP 响应码：200/403/480 等
    hangup_cause: Optional[str] = None      # FreeSWITCH 挂断原因：CALL_REJECTED / NO_ANSWER 等
    dial_attempts: int = 0                  # 外呼尝试次数

    @property
    def duration_seconds(self) -> Optional[int]:
        if self.answered_at and self.ended_at:
            return int((self.ended_at - self.answered_at).total_seconds())
        return None

    @property
    def is_active(self) -> bool:
        return self.state not in (CallState.ENDING, CallState.ENDED)


class StateMachine:
    """
    对话状态机
    根据 LLM 返回的意图和动作，决定下一步行为
    """

    # 动作别名归一化
    ACTION_ALIASES = {
        "request_human": "transfer",
        "human": "transfer",
        "transfer_to_human": "transfer",
        "hangup": "end",
        "terminate": "end",
        "sms": "send_sms",
        "call_back": "callback",
        "schedule_callback": "callback",
        "add_blacklist": "blacklist",
        "block": "blacklist",
    }

    # 当 action 缺失或无效时，按意图兜底
    INTENT_ACTIONS = {
        CallIntent.REQUEST_HUMAN: "transfer",
        CallIntent.CALLBACK: "callback",
    }

    # 连续拒绝几次后挂断
    MAX_REJECTIONS = 2

    def __init__(self, context: CallContext):
        self.ctx = context
        self._action_handlers: dict[str, Callable] = {}

    def register_handler(self, action: str, handler: Callable[..., Awaitable]):
        """注册动作处理函数（由 CallAgent 注入）"""
        self._action_handlers[action] = handler

    def transition(self, new_state: CallState):
        """状态转换，记录日志"""
        old = self.ctx.state
        self.ctx.state = new_state
        logger.debug(f"[{self.ctx.uuid}] 状态转换: {old.name} → {new_state.name}")

    def update_intent(self, intent_str: str):
        """更新用户意图"""
        try:
            self.ctx.intent = CallIntent(intent_str)
        except ValueError:
            self.ctx.intent = CallIntent.UNKNOWN
        logger.info(f"[{self.ctx.uuid}] 意图识别: {self.ctx.intent.name}")

    def _normalize_action(self, action: str) -> str:
        normalized = (action or "continue").strip().lower()
        return self.ACTION_ALIASES.get(normalized, normalized)

    async def process_llm_response(self, response: dict) -> tuple[str, str]:
        """
        处理 LLM 返回的结构化响应
        LLM 返回格式：
        {
          "reply": "您好，我们的产品...",      # 回复文本（用于 TTS）
          "intent": "interested",              # 识别到的用户意图
          "action": "continue|transfer|end",  # 下一步动作
          "action_params": {}                  # 动作参数
        }
        返回: (reply_text, action)
        """
        reply = response.get("reply", "")
        action = self._normalize_action(response.get("action", "continue"))
        intent_str = response.get("intent", "unknown")
        params = response.get("action_params", {})

        # 更新意图
        self.update_intent(intent_str)

        if action not in self._action_handlers and action != "continue":
            fallback_action = self.INTENT_ACTIONS.get(self.ctx.intent)
            if fallback_action:
                logger.warning(
                    f"[{self.ctx.uuid}] 未识别动作 {action!r}，按意图 {self.ctx.intent.value!r} 回退到 {fallback_action!r}"
                )
                action = fallback_action
            else:
                logger.warning(f"[{self.ctx.uuid}] 未识别动作 {action!r}，回退为 continue")
                action = "continue"

        # 拒绝计数
        if self.ctx.intent == CallIntent.NOT_INTERESTED:
            self.ctx.rejection_count += 1
            if self.ctx.rejection_count >= self.MAX_REJECTIONS:
                action = "end"

        # 执行动作
        if action in self._action_handlers:
            await self._action_handlers[action](params)

        return reply, action

    def should_continue(self) -> bool:
        return self.ctx.is_active and self.ctx.state != CallState.TRANSFERRING
