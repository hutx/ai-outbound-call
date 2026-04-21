# 通话详情记录设计

**日期**: 2026-04-21
**状态**: 待审核

## 需求概述

记录每次外呼通话的完整交互详情，包括：
- 每轮对话的用户说话内容、语音时长
- 每轮对话的机器人回复内容、播报时长
- 是否被打断、打断时间点、打断时识别的文本
- 无响应事件的触发时间和追问次数
- 通话结束原因和结束语

## 设计原则

- 零依赖：不引入新框架或第三方库
- 向后兼容：不影响现有业务逻辑，只增加数据记录
- 轻量内存：通话过程中驻留内存，结束时序列化持久化
- 可扩展：后续可对接分析系统（通话质量评估、用户行为分析）

## 架构

### 新增数据结构（`state_machine.py`）

```python
@dataclass
class TurnRecord:
    """一轮完整对话的记录"""
    turn: int = 0                   # 轮次（从1开始）
    user_text: str = ""             # 用户说的内容
    user_speech_ms: float = 0       # 用户说话时长（ASR 检测到的语音时长）
    user_at_ms: float = 0           # 用户说话时间点（相对于通话接通）
    ai_text: str = ""               # AI 回复内容
    ai_speech_ms: float = 0         # AI 播报时长（基于 sent_bytes / 16 计算）
    ai_at_ms: float = 0             # AI 播报开始时间点（相对于通话接通）
    barge_in: bool = False          # 是否被打断
    barge_in_at_ms: float = 0       # 打断时间点（相对于 AI 播报开始）
    barge_in_text: str = ""         # 打断时识别到的文本
    no_response: bool = False       # 是否无响应（用户无文本输出）
    asr_timeout: bool = False       # ASR 是否超时


@dataclass
class NoResponseEvent:
    """一次无响应追问事件"""
    count: int                      # 第几次无响应
    at_ms: float                    # 触发时间点（相对于通话接通）
    prompt_text: str                # 追问文本（如"您好，请问您还在吗？"）


@dataclass
class CallTranscript:
    """通话完整记录"""
    turns: list[TurnRecord] = field(default_factory=list)
    no_response_events: list[NoResponseEvent] = field(default_factory=list)
    total_duration_ms: float = 0    # 通话总时长
    end_reason: str = ""            # 结束原因：timeout / max_no_response / user_reject / end_action / transfer / error
    end_closing_text: str = ""      # 结束语内容
```

### 挂载到 CallContext

```python
@dataclass
class CallContext:
    # ... 现有字段 ...
    transcript: CallTranscript = field(default_factory=CallTranscript)
```

### 记录时机与注入点

| 事件 | 注入代码位置 | 记录内容 |
|------|-------------|----------|
| 用户说话 | `_conversation_loop` 中 `_listen_user` 返回后 | `TurnRecord` 的 `user_text`, `user_speech_ms`（`adapter.audio_stats().speech_ms`）, `no_response`, `asr_timeout` |
| AI 播报 | `_say` 返回后 | `ai_text`, `ai_speech_ms`（`sent_bytes / 16`） |
| 打断发生 | `_say` 中 `barge_in_detected=True` | `barge_in=True`, `barge_in_at_ms`, `barge_in_text` |
| 无响应追问 | `_conversation_loop` 无响应分支 | `NoResponseEvent` |
| 通话结束 | `run()` 的 finally 块 | `total_duration_ms`, `end_reason`, `end_closing_text` |

### 辅助工具类（`call_agent.py` 内部）

在 `CallAgent` 上增加一个私有方法 `_record_turn()` 和 `_record_no_response()`，封装记录逻辑，避免在业务代码中散落 `transcript` 字段赋值：

```python
def _record_turn(self, turn: int, **kwargs):
    """记录一轮对话"""
    record = TurnRecord(turn=turn, **kwargs)
    self.ctx.transcript.turns.append(record)

def _record_no_response(self, count: int, at_ms: float, prompt_text: str):
    """记录一次无响应事件"""
    event = NoResponseEvent(count=count, at_ms=at_ms, prompt_text=prompt_text)
    self.ctx.transcript.no_response_events.append(event)
```

### 持久化

通话结束后，在 `_cleanup()` 中序列化 Transcript：

```python
import json

transcript_path = f"{config.freeswitch.recording_path}/{self.ctx.task_id}/{self.ctx.uuid}.transcript.json"
os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
with open(transcript_path, "w", encoding="utf-8") as f:
    json.dump(dataclasses.asdict(self.ctx.transcript), f, ensure_ascii=False, indent=2)
```

输出示例：

```json
{
  "turns": [
    {
      "turn": 1,
      "user_text": "你好，什么事？",
      "user_speech_ms": 1200.0,
      "user_at_ms": 3500.0,
      "ai_text": "您好，我是XX客服，通知您一个优惠活动。",
      "ai_speech_ms": 4200.0,
      "ai_at_ms": 4800.0,
      "barge_in": true,
      "barge_in_at_ms": 1500.0,
      "barge_in_text": "等等，我在开会",
      "no_response": false,
      "asr_timeout": false
    },
    {
      "turn": 2,
      "user_text": "",
      "user_speech_ms": 0,
      "user_at_ms": 12000.0,
      "ai_text": "",
      "ai_speech_ms": 0,
      "ai_at_ms": 12000.0,
      "barge_in": false,
      "barge_in_at_ms": 0,
      "barge_in_text": "",
      "no_response": true,
      "asr_timeout": true
    }
  ],
  "no_response_events": [
    {
      "count": 1,
      "at_ms": 12000.0,
      "prompt_text": "您好，请问您还在吗？"
    }
  ],
  "total_duration_ms": 65000.0,
  "end_reason": "timeout",
  "end_closing_text": "您本次通话时长已结束。感谢您的接听，再见！"
}
```

## 错误处理

- **记录失败不阻断通话**：所有记录操作包裹在 `try/except` 中，异常只打日志
- **空值兼容**：`TurnRecord` 所有字段有默认值，缺失数据时不会崩溃
- **序列化异常**：如果 JSON 写入失败，至少日志中有 transcript 的 debug 输出

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `backend/core/state_machine.py` | 修改 | 新增 `TurnRecord`, `NoResponseEvent`, `CallTranscript` 数据类；`CallContext` 新增 `transcript` 字段 |
| `backend/core/call_agent.py` | 修改 | 在 `_conversation_loop`, `_say`, `_say_timeout_closing`, `run()` 中注入记录调用 |

## 影响范围评估

- 不涉及 FreeSWITCH 配置变更
- 不涉及 ASR/TTS/LLM 接口变更
- 不涉及现有对话流程逻辑变更
- 纯附加操作：记录不影响行为
