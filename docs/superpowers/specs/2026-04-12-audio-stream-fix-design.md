# 外呼接通后收不到用户音频 - 根因分析与修复

**日期**: 2026-04-12
**问题**: 外呼接通后只能收到 TTS 音频，收不到用户语音

## 架构概述

```
用户(分机1001/A-leg) ← bridge → loopback/AI_CALL(B-leg)
                                          ↓
                                    Outbound ESL Socket → backend:9999
                                          ↓
                                    uuid_audio_stream → ws://backend:8765
```

originate 命令: `originate {vars}user/1001@domain &bridge(loopback/AI_CALL)`

- A-leg: sofia 通道 (用户分机 1001)，UUID 由系统分配 (如 `8e7374b3`)
- B-leg: loopback 通道 (AI_CALL)，Outbound Socket 连接在此通道上 (如 `685e81f2`)

## 根因分析

### 问题 1: `connect()` 中 `uuid_audio_stream` 目标 UUID 错误

`ESLSocketCallSession.connect()` 在 Outbound Socket 握手时启动 `uuid_audio_stream`，使用以下逻辑选择目标 UUID:

```python
stream_uuid = (
    self._channel_vars.get("other_loopback_leg_uuid")
    or self._channel_vars.get("signal_bond")    # ← 匹配到 sofia A-leg
    or self._uuid                                # ← loopback B-leg
)
```

`signal_bond` 指向 sofia A-leg (用户分机)。但 sofia 通道上的 `uuid_audio_stream` 只捕获端点原始音频，在 bridge 场景下无法捕获桥接后的用户语音。日志确认：**sofia A-leg 上的流收到 0 帧，0 bytes**。

而后续 `start_audio_capture()` 在 loopback B-leg 上启动的流收到了 **295 帧，94400 bytes (~5.9s 音频)**。

### 问题 2: AudioStreamAdapter 在用户语音到达前退出

Adapter 的 `stream()` 方法有一个 15 秒的初始等待超时：

```python
if not self._started_speaking and initial_wait_ms >= 15000:
    break
```

当音频流开始推送后，Adapter 消费了前 10 个 chunk（TTS 音频尾部，max_rms=53，低于阈值 250），然后超时退出。**用户语音帧（max_rms=4710）留在队列中未被消费。**

### 问题 3: `_on_call_answered` 在错误的通道上启动音频流

对于内部分机 originate，`_on_call_answered` 在 loopback 通道上启动 `uuid_audio_stream`，但该通道在 bridge 完成前没有音频流过。

## 修复方案

### 修复 1: `connect()` 中直接使用当前通道 (loopback B-leg)

**文件**: `backend/services/esl_service.py`

```python
# 修改前: 使用 signal_bond → sofia A-leg (0 帧)
stream_uuid = (
    self._channel_vars.get("other_loopback_leg_uuid")
    or self._channel_vars.get("signal_bond")
    or self._uuid
)

# 修改后: 直接使用当前通道 (loopback B-leg) → 295 帧
stream_uuid = self._uuid
```

loopback B-leg 能捕获 bridge 后的混合音频（TTS + 用户语音）。

### 修复 2: AudioStreamAdapter 等待队列就绪后再消费

**文件**: `backend/core/call_agent.py`

```python
# 新增: 等待队列有数据后再开始 VAD 处理
ready_deadline = time.time() + 10.0
while time.time() < ready_deadline and not self._stopped:
    if not self._queue.empty():
        break
    await asyncio.sleep(0.1)

# 修改: 初始等待从 15s 缩短到 5s
if not self._started_speaking and initial_wait >= 5000:
    break
```

### 修复 3: `start_audio_capture` 优先使用 A-leg UUID

**文件**: `backend/services/esl_service.py`

如果 `export_origination_uuid` 可用，优先在 A-leg 上启动音频流，直接捕获用户语音入口。

## 验证方法

1. 发起内部分机外呼 (拨打 1001)
2. 检查日志中 `uuid_audio_stream 目标` 是否为 loopback B-leg UUID
3. 检查 `音频帧 #1` 日志确认帧持续到达
4. 检查 `speech_detected` 日志确认识别到用户语音
5. 检查 `/recordings/debug/asr_input_*.wav` 确认包含用户语音
