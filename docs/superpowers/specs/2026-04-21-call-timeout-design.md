# 通话时长限制功能设计

**日期**: 2026-04-21
**状态**: 待审核

## 需求概述

AI 外呼通话设置默认 5 分钟的最大通话时长，到达时长前 20 秒触发超时结束流程：打断正在播报的 TTS，播放超时结束语后结束通话。

## 设计原则

- 复用现有的 `_barge_in`（`asyncio.Event`）信号模式，最小化改动
- 通话时长可通过环境变量配置，缓冲时间也可配置
- 超时结束语采用硬编码格式：`"您本次通话时长已结束。" + closing_script`

## 配置

### `backend/core/config.py`

新增两个环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MAX_CALL_DURATION_SECONDS` | 300 | 总通话时长（秒） |
| `CALL_END_BUFFER_SECONDS` | 20 | 超时触发前的缓冲时间（秒） |

触发点 = `MAX_CALL_DURATION_SECONDS - CALL_END_BUFFER_SECONDS`（默认 280 秒 = 4 分 40 秒）。

## 架构

### `CallAgent` 新增属性

```python
self._call_timeout = asyncio.Event()          # 通话超时信号
self._call_timeout_task: Optional[Task] = None  # 看门狗任务
```

### `_closing_script` 的来源

`self._script_config` 在 `CallAgent.run()` 中已有（第 315 行），直接从 `self._script_config.closing_script` 读取。

### 看门狗任务

`_start_timeout_watchdog()` — 电话接通后启动，在 `run()` 的 `session.connect()` 之后调用。

`_timeout_watchdog(trigger_at)` — 独立协程，`asyncio.sleep(trigger_at)` 后设置 `self._call_timeout` 事件。

### 对话循环改造

`_conversation_loop()` 中每次循环开始时检查：

```python
if self._call_timeout.is_set():
    await self._stop_tts_if_playing()
    await self._say_timeout_closing()
    break
```

在 `_say()` 返回后、`_think_and_reply_stream()` 开始前也检查一次，防止 LLM 流式输出期间超时。

### 超时触发行为

1. **打断 TTS** — `_stop_tts_if_playing()` 调用 `session.stop_playback()` 停止 FreeSWITCH TTS 播放，取消残留的 `_barge_in_asr_task`
2. **播放结束语** — `_say_timeout_closing()` 发送 `"您本次通话时长已结束。" + closing_script`，如果 `closing_script` 为空则 fallback 到 `"感谢您的接听，再见！"`
3. **等待播放完成** — 动态等待 `max(len(hangup_msg) * 150, 2000)ms`
4. **结束通话** — `break` 退出 `_conversation_loop()`，进入正常的 `run()` finally 清理

### 清理

`CallAgent.run()` 的 `finally` 块中：如果 `_call_timeout_task` 未结束，调用 `.cancel()`。

## 错误处理

- **看门狗任务异常** — `try/except` 包裹，异常降级为立即 `self._call_timeout.set()` 触发
- **`stop_playback()` 失败** — 静默跳过，继续发送结束语
- **通话在超时前已挂断** — `run()` 的 finally 中取消看门狗任务
- **`closing_script` 为空** — fallback 到 `"感谢您的接听，再见！"`

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `backend/core/config.py` | 修改 | 新增 2 个环境变量配置 |
| `backend/core/call_agent.py` | 修改 | 替换现有 `asyncio.wait_for(..., MAX_CALL_DURATION)` 超时逻辑为看门狗 + 信号模式；新增 `_start_timeout_watchdog`、`_timeout_watchdog`、`_stop_tts_if_playing`、`_say_timeout_closing`；删除旧 `MAX_CALL_DURATION` 常量 |
