# CallAgent 打断（Barge-in）与宽容期（Tolerance）机制深度分析报告

## 执行摘要

本项目的智能外呼系统实现了三层核心机制确保对话流畅性和用户体验：

1. **Barge-in（打断）机制**：用户可在AI播报期间打断并立即被识别
2. **Tolerance（宽容期）机制**：用户完成首次表达后允许补充内容的时间窗口
3. **No-Response（无响应）处理**：连续或累积的用户无响应触发重试或挂断

---

## 1. 打断机制（Barge-in/Interruption）

### 1.1 机制定义与作用

**打断机制目的**：在 AI 通过 TTS 播报内容时，一旦用户开始说话，系统应立即停止 TTS 播报，切换到监听用户输入的模式。

**设计理念**：模拟真实通话中"用户插话"的自然行为，提升交互体验感。

### 1.2 完整工作流程（时间序列）

```
时间轴：
┌─────────┬──────────────┬──────────┬──────────┬─────────┐
│ 用户开始 │ 保护期       │ 有效      │ 保护期   │ 播放完  │
│ 说话     │ (start)      │ 打断区间  │ (end)    │ 成      │
├─────────┼──────────────┼──────────┼──────────┼─────────┤
│ ▼       │ ✗ 忽略       │ ✓ 打断   │ ✗ 忽略   │        │
│ ASR识别 │ (protect_    │ 触发      │ (protect │        │
│         │ start_ms)    │          │ _end_ms) │        │
└─────────┴──────────────┴──────────┴──────────┴─────────┘
0ms                                          duration_ms
```

### 1.3 核心数据结构

**配置参数**（来自CallScript数据表）：
```python
opening_barge_in: bool              # 开场白是否支持打断（默认False）
closing_barge_in: bool              # 结束语是否支持打断（默认False）
conversation_barge_in: bool         # 对话阶段是否支持打断（默认True）
barge_in_protect_start: int         # 播放起始保护期（秒，默认3）
barge_in_protect_end: int           # 播放结束保护期（秒，默认3）
```

**运行时控制对象**（CallAgent）：
```python
_barge_in: asyncio.Event            # 打断触发信号（初始置位=未打断）
_barge_in_text: str                 # 打断时识别到的文本
_barge_in_asr_task: Task            # 后台ASR监听任务
_last_barge_in_config: dict         # 上一次_say的打断配置缓存
_last_say_had_barge_in: bool        # 标记是否发生过打断（用于TTS管道恢复）
```

### 1.4 触发条件与检测

**打断检测的三层验证**：

1. **噪声过滤**（防止误触发）
   - 语气词：嗯、哦、啊、呃、哎、喂、好、是、对、行、你
   - 英文噪声：ok、yes、no、system
   - 纯英文短词（无中文字符，长度<10）
   - 重复字符模式（如"嗯嗯。"）
   - 逗号分隔的语气词组合（如"嗯，对吧。"）

2. **能量检查**（RMS值过滤）
   ```python
   if adapter._max_rms < 1500:  # 能量过低，过滤
   ```

3. **保护期检查**（时间窗口过滤）
   ```python
   elapsed_ms = (time.time() - tts_start_time) * 1000
   if elapsed_ms < protect_start_ms:
       # 在起始保护期内，忽略打断
       continue
   if elapsed_ms > (total_duration_ms - protect_end_ms):
       # 在结束保护期内，忽略打断
       continue
   ```

### 1.5 关键代码路径

#### 路径1：启动打断监听
```python
# CallAgent._say() 方法中
async def _start_barge_in_listener(self, protect_start_ms: int, tts_start_time: float):
    audio_capture_task = asyncio.create_task(self.session.start_audio_capture())
    self._barge_in_asr_task = asyncio.create_task(
        self._main_asr_barge_in_loop(
            audio_capture_task,
            protect_start_ms=protect_start_ms,
            tts_start_time=tts_start_time,
        )
    )
```

#### 路径2：监听打断检测
```python
# _main_asr_barge_in_loop 检测流程
async def _main_asr_barge_in_loop(...):
    async for result in self._asr_with_retry(adapter.stream()):
        if not result.is_final or not result.text:
            continue
        
        # 第1层：噪声过滤
        if _is_noise(result.text):
            continue
        
        # 第2层：保护期检查
        elapsed_ms = (time.time() - tts_start_time) * 1000
        if elapsed_ms < protect_start_ms:
            continue
        
        # 第3层：触发打断
        self._barge_in_text = result.text
        self._barge_in.set()
        await self.session.stop_playback()  # 立即停止TTS
        break
```

#### 路径3：在_say中检查打断
```python
async def _say(self, text: str, ...) -> tuple[bool, str, int]:
    self._barge_in.clear()  # 开始播放，启用检测
    
    async for chunk in audio_chunks:
        if self._barge_in.is_set():
            # 打断被检测到
            await self.session.stop_playback()
            return True, self._get_barge_in_text(), sent_bytes
        # 发送chunk
        await self.session.forkzstream_ws_server.send_audio(...)
    
    # 播放完成后
    self._barge_in.set()  # 禁用检测
    return False, "", sent_bytes
```

---

## 2. 宽容期机制（Tolerance）

### 2.1 宽容期的定义与用途

**定义**：用户完成首次表达后，系统允许其继续补充或修正的时间窗口。

**用途**：捕捉用户分段表达的情况，如"我想查"（第一句）+ "余额"（补充）。

### 2.2 配置参数

```python
tolerance_enabled: bool = True              # 是否启用宽容期
tolerance_ms: int = 1000                    # 宽容期时长（毫秒）
```

### 2.3 实现位置与流程

#### 宽容期触发点
```python
# _conversation_loop 中
tolerance_config = await get_barge_in_config(...)
tolerance_enabled = tolerance_config.get("tolerance_enabled", False)
tolerance_ms = tolerance_config.get("tolerance_ms", 1000)

if tolerance_enabled:
    # 并行启动：LLM处理 + 宽容期监听
    llm_task = asyncio.create_task(self._think_and_reply_stream(user_text))
    tolerance_task = asyncio.create_task(self._listen_tolerance(tolerance_ms))
    
    done, pending = await asyncio.wait(
        [llm_task, tolerance_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
```

#### 宽容期监听实现
```python
async def _listen_tolerance(self, tolerance_ms: int) -> str:
    try:
        async with asyncio.timeout(tolerance_ms / 1000.0):
            # 重新启动音频捕获和ASR
            audio_queue = await self.session.start_audio_capture()
            # 清空残留音频
            while not audio_queue.empty():
                audio_queue.get_nowait()
            
            # 流式ASR识别
            async for result in self._asr_with_retry(adapter.stream()):
                if result.is_final and result.text:
                    # 噪声过滤
                    if _is_noise(result.text):
                        continue
                    # 能量过滤
                    if adapter._max_rms < 1500:
                        continue
                    # 有效补充文本
                    return result.text
    except asyncio.TimeoutError:
        pass
    
    return ""
```

### 2.4 与LLM流式处理的并发

```python
# 当tolerance_task先完成时（检测到补充文本）
if tolerance_task in done:
    new_text = tolerance_task.result()
    if new_text:
        combined = f"{user_text} {new_text}".strip()
        llm_task.cancel()
        reply_text, action = await self._think_and_reply_stream(combined)
    else:
        # 宽容期无新文本，等待LLM完成
        reply_text, action = await llm_task

# 当LLM先完成时
elif llm_task in done:
    # 取消宽容期任务
    tolerance_task.cancel()
    reply_text, action = await llm_task
```

---

## 3. 无响应处理（No Response）

### 3.1 无响应的定义与判定

**无响应判定条件**（在_listen_user中）：
```python
1. is_all_silent=True   # VAD全程未检测到语音
2. is_asr_timeout=True  # ASR等待超时
3. final_text=""        # 最终识别结果为空

# 但如果is_all_silent=True，则不计入无回应（继续监听）
if is_all_silent:
    logger.info("本轮全静音，不计入无回应，继续监听")
    continue
```

### 3.2 无响应配置参数

```python
no_response_timeout: int = 3              # 快速超时时长（秒）
no_response_mode: str = "consecutive"     # 重试模式
no_response_max_count: int = 3            # 最大无回应次数
no_response_hangup_msg: str | None        # 自定义挂断语
no_response_hangup_enabled: bool = True   # 是否启用自定义挂断语
```

### 3.3 无响应重试模式

#### 模式1：Consecutive（连续模式）
```
规则：检测到用户有效回应时，计数器重置为0

流程：
循环1: 无回应（count=1）→ 追问1
循环2: 有有效回应 → count=0重置
循环3: 无回应（count=1）→ 追问1
```

#### 模式2：Cumulative（累计模式）
```
规则：累计所有无回应次数，达到阈值时挂断

流程：
循环1: 无回应（count=1）→ 追问1
循环2: 有有效回应 → count保持=1（不重置）
循环3: 无回应（count=2）→ 追问2
```

### 3.4 无响应处理流程

```python
if not user_text:
    if is_all_silent:
        logger.info("本轮全静音，不计入无回应")
        continue
    
    no_response_count += 1
    
    if no_response_count >= no_response_max_count:
        # 播放挂断语
        if no_response_hangup_enabled and no_response_hangup_msg:
            hangup_msg = no_response_hangup_msg
        elif closing_script:
            hangup_msg = closing_script
        else:
            hangup_msg = "感谢接听，再见！"
        
        # 播放并等待
        _, _, sent_bytes = await self._say(hangup_msg, ...)
        await asyncio.sleep(wait_ms / 1000.0)
        break
    
    # 播放追问语
    await self._say("您好，请问您还在吗？")
    
    # 快速超时检查
    quick_timeout = no_response_config.get("timeout", 3)
    user_text, is_all_silent, is_asr_timeout = await self._listen_user(
        quick_timeout=quick_timeout,
        ...
    )

# 用户有有效回应时的计数重置
if user_text and no_response_mode == "consecutive":
    no_response_count = 0
```

---

## 4. 状态机与各机制的交互

### 4.1 关键状态定义

```python
class CallState(Enum):
    DIALING        # 拨号中
    RINGING        # 振铃中
    CONNECTED      # 已接通
    AI_SPEAKING    # AI正在播放语音 ← 打断检测发生在此
    USER_SPEAKING  # 用户正在说话 ← 切换到此
    PROCESSING     # LLM推理中
    TRANSFERRING   # 转人工中
    ENDING         # 正在结束
    ENDED          # 已结束
```

### 4.2 打断触发后的状态转换

```python
if barge_in_detected:
    self._last_say_had_barge_in = True
    self._stop_barge_in_listener()
    barge_text = self._get_barge_in_text()
    
    # 在_conversation_loop中处理
    if barge_in_occurred and barge_text:
        # 使用barge-in文本走LLM
        user_text = barge_text
        reply_text, action = await self._think_and_reply_stream(user_text)
```

---

## 5. 通话超时与打断的协作

### 5.1 超时看门狗机制

```python
def _start_timeout_watchdog(self):
    total = config.max_call_duration_seconds  # 默认300s
    buffer = config.call_end_buffer_seconds    # 默认20s
    trigger_at = max(total - buffer, 1)       # 触发时间点
    
    self._call_timeout_task = asyncio.create_task(
        self._timeout_watchdog(trigger_at)
    )

async def _timeout_watchdog(self, trigger_at: float):
    try:
        await asyncio.sleep(trigger_at)
        self._call_timeout.set()  # 设置超时信号
    except asyncio.CancelledError:
        pass
```

### 5.2 超时与打断的交互

```python
if self._call_timeout.is_set():
    await self._stop_tts_if_playing()      # 停止TTS
    await self._say_timeout_closing()      # 播放超时结束语
    break

async def _stop_tts_if_playing(self):
    await self.session.stop_playback()
    self._last_say_had_barge_in = True     # 标记需要恢复
    self._stop_barge_in_listener()         # 取消后台ASR
```

---

## 6. 音频流与打断的底层交互

### 6.1 AudioStreamAdapter的VAD与打断检测

```python
class AudioStreamAdapter:
    """
    将FreeSWITCH音频Queue转为ASR可消费的AsyncGenerator
    支持barge-in检测：AI播报期间检测到有声帧即触发打断
    """
    
    def __init__(self, audio_queue, vad_silence_ms=500, ...):
        self._barge_in_event = barge_in_cb        # 打断事件
        self._started_speaking = False            # 用户是否开始说话
        self._is_all_silent = False               # 流结束时：从未检测到语音
        self._consecutive_speech_frames = 0       # 连续语音帧计数
        self._speech_onset_threshold = 2          # 2帧=40ms才认为开始说话
        self._vad = SimpleVAD(...)                # VAD引擎

async def stream(self) -> AsyncGenerator[bytes, None]:
    # 两段式VAD：首次等待超时（2s）+ 说话中VAD（500ms）
    while not self._stopped:
        chunk = await self._queue.get()
        rms = self._vad.frame_rms(chunk)
        has_speech = rms > self._vad.energy_threshold
        
        if has_speech:
            self._consecutive_speech_frames += 1
            if self._consecutive_speech_frames >= self._speech_onset_threshold:
                if not self._started_speaking:
                    self._started_speaking = True
                    # 触发打断检测
```

---

## 7. 配置参数速查表

### 7.1 打断配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| opening_barge_in | bool | False | 开场白是否支持打断 |
| closing_barge_in | bool | False | 结束语是否支持打断 |
| conversation_barge_in | bool | True | 对话阶段是否支持打断 |
| barge_in_protect_start | int | 3 | 播放起始保护期（秒） |
| barge_in_protect_end | int | 3 | 播放结束保护期（秒） |

### 7.2 宽容期配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| tolerance_enabled | bool | True | 是否启用宽容期 |
| tolerance_ms | int | 1000 | 宽容期时长（毫秒） |

### 7.3 无响应配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| no_response_timeout | int | 3 | 快速超时时长（秒） |
| no_response_mode | str | "consecutive" | 重试模式 |
| no_response_max_count | int | 3 | 最大无回应次数 |
| no_response_hangup_msg | str | NULL | 自定义挂断语 |
| no_response_hangup_enabled | bool | True | 是否启用自定义挂断语 |

### 7.4 全局超时配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| max_call_duration_seconds | int | 300 | 单路通话最长时长（秒） |
| call_end_buffer_seconds | int | 20 | 超时触发缓冲时间（秒） |
| ASR_TIMEOUT | const | 15.0 | ASR单句最长等待（秒） |
| LLM_TIMEOUT | const | 30.0 | LLM推理超时（秒） |
| TTS_TIMEOUT | const | 10.0 | TTS单chunk超时（秒） |
| VAD_SILENCE_MS | const | 400 | VAD静音判定时长（毫秒） |

---

## 8. 可能的边界情况与潜在问题

### 8.1 打断相关

#### 情景1：快速连续打断
```
问题：用户快速说两句话，第一句被识别为打断
处理：打断后正常走LLM，第二句在下一轮_listen_user捕获
```

#### 情景2：打断在保护期边界
```
问题：elapsed_ms恰好等于protect_start_ms=3000
当前代码：if elapsed_ms < protect_start_ms
判断结果：3000 < 3000 = False，会触发打断 ✓
```

#### 情景3：打断时TTS已播完
```
问题：ASR识别慢，打断信号到达时TTS已播完
处理：_say()末尾检查self._barge_in.is_set()，
      若已被设置则立即返回True
```

### 8.2 宽容期相关

#### 情景1：宽容期内恰好收到空文本
```
处理：_listen_tolerance()超时返回""
      当tolerance_task完成返回""时，等待llm_task完成
```

#### 情景2：宽容期内全噪声
```
处理：_listen_tolerance()中有噪声过滤
      若连续过滤5次则退出，返回""
```

### 8.3 无响应相关

#### 情景1：VAD误判全静音
```
当前处理：直接continue，不计入无响应 ✓
风险：若系统环境持续很安静，可能导致无限等待
规避：通常配置ASR_TIMEOUT=15s确保超时退出
```

#### 情景2：追问后快速超时=3s
```
场景：追问后启用quick_timeout=3s
问题：用户可能在第4秒才开始说话
处理：判定无响应+1，如此重复3次后挂断
改进建议：可增加"反应迟钝"模式或降低quick_timeout
```

#### 情景3：Cumulative模式计数不重置
```
风险：可能显得过于严格
示例：
循环1：无回应(count=1) → 追问1
循环2：有回应"好的" → count保持=1
循环3：无回应(count=2) → 追问2
循环4：无回应(count=3) → 挂断
```

### 8.4 超时与其他机制的交互

#### 情景1：超时时TTS正在流式播报
```
处理（第536-540行）：
检查if self._call_timeout.is_set()
调用_stop_tts_if_playing()
播放超时结束语并结束通话
```

#### 情景2：超时时在宽容期等待中
```
处理（第466-481行）：
在tolerance_task/llm_task之外加入timeout_wait_task
若timeout先触发，取消所有pending任务
调用_stop_tts_if_playing() + _say_timeout_closing()
```

---

## 9. 工程最佳实践

### 9.1 打断配置

- 开场白通常不打断（让用户充分理解）
- 结束语通常不打断（防止误打断挂断流程）
- 对话中应启用打断（提升交互体感）
- protect_start_ms=3s可避免"开场白被立即打断"

### 9.2 宽容期配置

- 用户常见分段表达场景启用
- 1000-2000ms为推荐值
- 可根据用户人群调整

### 9.3 无响应配置

- Consecutive：用户反应慢但最后配合
- Cumulative：需要纯净输入，容错度低
- max_count通常设置为3（平衡友好度和效率）
- 自定义挂断语应包含感谢和道歉

### 9.4 超时配置

- 运营商通常5-10分钟后断线
- 建议总时长300s，缓冲20s
- 确保最后的挂断语有时间播报

---

## 10. 调试建议

启用DEBUG模式，查看以下关键日志：
```
[uuid] ⚡ barge-in 触发(is_final)        # 打断检测
[uuid] 宽容期收到补充语音               # 宽容期
[uuid] 第 N 次无回应，追问               # 无响应
[uuid] 通话超时信号设置                 # 超时
```

---

## 11. 关键文件位置

| 功能 | 文件 | 行号 |
|------|------|------|
| CallAgent主类 | backend/core/call_agent.py | 215-1501 |
| AudioStreamAdapter | backend/core/call_agent.py | 48-213 |
| 打断检测 | backend/core/call_agent.py | 855-943 |
| 宽容期监听 | backend/core/call_agent.py | 777-853 |
| 无响应判定 | backend/core/call_agent.py | 392-450 |
| 超时看门狗 | backend/core/call_agent.py | 1380-1436 |
| 状态机 | backend/core/state_machine.py | 1-202 |
| 配置获取 | backend/services/async_script_utils.py | 128-210 |
| 数据模型 | backend/models/call_script.py | 1-44 |
| 配置管理 | backend/core/config.py | 1-206 |
| 迁移脚本-宽容期 | backend/scripts/migrate_add_tolerance_fields.py | |
| 迁移脚本-无响应 | backend/scripts/migrate_add_no_response_fields.py | |

