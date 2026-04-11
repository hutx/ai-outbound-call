# AI 外呼系统架构文档

## 一、整体架构

```
┌──────────────┐   SIP/RTP    ┌─────────────────────────────┐
│              │ ◄──────────► │                             │
│  Zoiper/SIP  │              │  FreeSWITCH                 │
│  终端(1001)  │              │  sofia B-leg                │
│              │              │     ↔ loopback-a             │
└──────────────┘              │     ↔ loopback-b(socket)    │
                              │         │                    │
                              │         │ ESL Socket(:9999)  │
                              └─────────┼───────────────────┘
                                        │
                              ESL Inbound(:8021)            ┌──────────────────┐
                              uuid_broadcast (TTS 播放)      │                  │
                                        │                  │  后端 Python      │
                              WebSocket(:8765)  ◄──────────► │  (AI 外呼系统)  │
                              uuid_audio_stream              │                  │
                              (音频流, 问题待解决)           │                  │
                                                            └────────┬─────────┘
                                                                     │
                                                           ┌─────────▼────────┐
                                                           │ ASR / LLM / TTS  │
                                                           │   云服务          │
                                                           └──────────────────┘
```

### 通话拓扑（内部分机外呼）

```
sofia B-leg(用户 1001) ↔ loopback-a ↔ loopback-b(socket → backend:9999)
  │                      │              │
  │ RTP ←→ 用户麦克风     │ 媒体流经此处   │ 由后端 CallAgent 控制
  │                       │              │
  └─ uuid_broadcast ──────┴──────────────┘ (TTS 播放到这里)
```

### 四条通信通道

| 通道 | 端口 | 方向 | 用途 |
|------|------|------|------|
| SIP/RTP | 5060/16384-32768 | 双向 | Zoiper ↔ FreeSWITCH 信令和媒体流 |
| audio_stream WebSocket | 8765 | FS → Backend | mod_audio_stream 推送用户麦克风 PCM 音频 |
| ESL Outbound Socket | 9999 | FS → Backend | FreeSWITCH 主动连入后端，传递通话控制 |
| ESL Inbound Pool | 8021 | Backend → FS | 后端发起 ESL API 调用（originate、uuid_broadcast） |

---

## 二、FreeSWITCH 核心配置

### 2.1 模块加载

**文件**: `freeswitch/conf/autoload_configs/modules.conf.xml`

```xml
<load module="mod_audio_stream"/>
```

`mod_audio_stream` 是核心模块，负责将通道音频实时推送到 WebSocket。

### 2.2 audio_stream 模块配置

**文件**: `freeswitch/conf/autoload_configs/audio_stream.conf.xml`

```xml
<configuration name="audio_stream.conf" description="实时音频流推送">
  <settings>
    <param name="default-url"     value="ws://127.0.0.1:8765"/>
    <param name="default-rate"    value="8000"/>
    <param name="default-format"  value="L16"/>
    <param name="default-ms"      value="20"/>
    <param name="default-channel" value="read"/>
    <param name="connect-timeout" value="3000"/>
    <param name="reconnect-interval-ms" value="1000"/>
  </settings>
</configuration>
```

| 参数 | 值 | 说明 |
|------|-----|------|
| default-rate | 8000 | 标准电话采样率，与 ASR 配置一致 |
| default-channel | read | 只捕获读方向音频（用户麦克风），不混入 TTS 播放音频 |
| default-ms | 20 | 每 20ms 推送一帧，实时语音标准 |
| default-format | L16 | 16bit 线性 PCM |

### 2.3 SIP Profile 配置

**文件**: `freeswitch/conf/autoload_configs/sofia.conf.xml`

关键配置：

```xml
<profile name="internal">
    <param name="sip-port" value="$${internal_sip_port}"/>   <!-- 5060 -->
    <param name="context"  value="internal"/>                 <!-- 进入 internal 拨号计划 -->
    ...
</profile>
```

- 内部分机（1001-1019）注册到 `internal` profile
- 用户 `user_context=internal`（`directory/default.xml`）
- 外呼时通过 `user/{ext}@{domain}` 查找注册信息，B-leg 自动进入 `internal` context

### 2.4 全局变量

**文件**: `freeswitch/conf/vars.xml`

```xml
<X-PRE-PROCESS cmd="set" data="backend_socket_host=backend" />
<X-PRE-PROCESS cmd="set" data="backend_socket_port=9999" />
```

### 2.5 拨号计划（Dialplan）

外呼通过 originate 的 `&bridge(loopback/AI_CALL)` 执行，不需要内部分机拨号计划匹配。

`freeswitch/conf/dialplan/default.xml` 中的 `ai_call_handler` 处理 loopback-b：

---

## 三、后端实现

### 3.1 组件概览

```
┌─────────────────────────────────────────────────────────────────┐
│  backend/api/main.py  (FastAPI 入口)                            │
│                                                                 │
│  启动顺序:                                                       │
│  1. 数据库初始化                                                  │
│  2. CRM 服务（黑名单预热）                                        │
│  3. ASR / TTS / LLM 服务初始化                                    │
│  4. AudioStreamWebSocket 监听 :8765（接收 mod_audio_stream）     │
│  5. ESL Inbound Pool 连接 :8021（发起 originate、uuid_broadcast）│
│  6. ESL Outbound Server 监听 :9999（接受 FS 主动连入）           │
│  7. 任务调度器                                                    │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 ESL Inbound 连接池 — 发起外呼

**文件**: `backend/services/esl_service.py` — `AsyncESLConnection.originate()`

```python
async def originate(self, phone, call_uuid, task_id, script_id, ...) -> str:
    endpoint, endpoint_type, target_type = self._build_originate_target(...)

    channel_vars = (
        f"origination_uuid={call_uuid},"
        f"ai_agent=true,"
        f"export_ai_agent=true,"        # ← 导出到 B-leg
        f"export_task_id={task_id},"
        f"export_script_id={script_id},"
        f"export_call_target_type={target_type},"
        f"export_original_destination={phone},"
        f"export_origination_uuid={call_uuid},"
        ...
    )

    if endpoint_type == "internal_extension":
        # 内部分机：user/ 查找注册 → Zoiper 振铃 → bridge(loopback/AI_CALL)
        cmd = f"originate [{channel_vars}] {endpoint} &bridge(loopback/AI_CALL)"
    elif endpoint_type == "pstn":
        # PSTN 外呼
        cmd = f"originate [{pstn_vars}] {endpoint}"
```

#### 变量传递规则

| 变量前缀 | 作用域 | 示例 |
|----------|--------|------|
| `var=val` | 仅 A-leg | `origination_uuid=xxx` |
| `export_var=val` | 导出到 B-leg | `export_ai_agent=true` → B-leg 中 `ai_agent=true` |

#### 端点类型

| 类型 | 端点格式 | 说明 |
|------|----------|------|
| 内部分机 | `user/1001@{domain}` | directory 查找注册地址 → SIP INVITE |
| PSTN 外呼 | `sofia/gateway/{gw}/{number}` | 通过运营商网关 |

### 3.3 ESL Outbound Socket Server — 接受 FS 连入

**文件**: `backend/services/esl_service.py` — `ESLSocketCallSession.connect()`

FreeSWITCH 执行到 dialplan 的 `<action application="socket" ...>` 时，会主动 TCP 连接到后端 :9999。

```python
async def connect(self) -> dict:
    """完成握手，获取 channel 变量"""
    await self._send("connect\n\n")
    data = await self._read_event()

    # 提取 UUID
    self._uuid = data.get("Unique-ID") or data.get("Channel-Unique-ID")

    # 提取 channel 变量（variable_xxx → xxx）
    for k, v in data.items():
        if k.startswith("variable_"):
            self._channel_vars[k[9:]] = v

    # 订阅事件
    await self._send("myevents\n\n")
    await self._read_event()
    await self._send("divert_events on\n\n")

    # 音频捕获由 dialplan 中的 audio_stream 应用处理，
    # 此 socket 连接仅用于通话控制（TTS 播放、DTMF 等）
    return data
```

### 3.4 用户麦克风音频流 — WebSocket 接收

**文件**: `backend/services/audio_stream_ws.py` — `AudioStreamWebSocket`

```python
class AudioStreamWebSocket:
    """
    接收 mod_audio_stream 推送的音频帧

    工作流程:
      1. dialplan 执行 audio_stream → mod_audio_stream 连接此 WebSocket
      2. 首帧消息为 Channel UUID（纯文本）
      3. 后续每 20ms 推送一帧 PCM（8000Hz 16bit mono）
      4. PCM 帧直接送入全局 audio_queue
    """

    def __init__(self, host="0.0.0.0", port=8765, max_queue_size=500):
        self._global_queue: Optional[asyncio.Queue] = None  # 全局音频队列
        self._global_conn_count: int = 0                    # 活跃连接计数

    async def _handle_connection_simple(self, websocket):
        async for frame in websocket:
            if isinstance(frame, str):
                continue  # 首帧文本（Channel UUID），忽略

            # 二进制帧 = PCM 音频数据
            self._stats["frames_received"] += 1
            if queue.full():
                queue.get_nowait()  # 满时丢弃最老帧
            queue.put_nowait(frame)
```

全局队列设计：单通话场景最简单，无需匹配 UUID，任何连接的音频帧都放入同一队列。

### 3.5 CallAgent — 通话控制主流程

**文件**: `backend/core/call_agent.py`

```python
async def run(self):
    # 1. ESL 握手，读取 channel 变量
    await self.session.connect()

    # 2. 查询客户信息、构建话术
    self._system_prompt = await get_system_prompt_for_call(...)

    # 3. 启动事件监听
    event_task = asyncio.create_task(self.session.read_events())

    # 4. 主对话循环
    await self._conversation_loop()

async def _conversation_loop(self):
    # 播放开场白
    await self._say_opening()

    while self.session._connected:
        # 监听用户说话
        user_text = await self._listen_user(...)
        if not user_text:
            continue  # 无响应，重试

        # LLM 推理 + 回复
        reply_text, action = await self._think_and_reply(user_text)
        await self._say(reply_text)

        if action == "end":
            break
```

### 3.6 TTS 推送用户的完整流程

**文件**: `backend/core/call_agent.py` — `_say()` + `backend/services/esl_service.py` — `play()`

```
用户说话 → mod_audio_stream → ws://backend:8765 → audio_queue
                                                    │
                                                    ▼
LLM 生成回复文本 → TTS 服务合成 PCM → 写临时 WAV 文件
                                                    │
                                                    ▼
                                     uuid_broadcast {sofia_uuid} {wav_path} both
                                                    │
                                                    ▼ (ESL Inbound Pool → FS :8021)
                                     FreeSWITCH 播放 WAV 到 sofia channel
                                                    │
                                                    ▼ (RTP)
                                     Zoiper 听到 TTS 语音
```

#### 核心代码

```python
# call_agent.py — _say()
async def _say(self, text: str, record: bool = True, speech_type: str = "conversation"):
    # 1. TTS 流式合成 PCM
    pcm_parts = []
    async for chunk in self._tts_stream_with_timeout(text):
        if chunk:
            pcm_parts.append(chunk)

    # 2. 写临时 WAV 文件（8000Hz 16bit mono）
    pcm_data = b"".join(pcm_parts)
    write_wav(temp_path, pcm_data, sample_rate=8000)

    # 3. 通过 ESL Inbound Pool 播放
    await self.session.play(temp_path, timeout=60.0)
```

```python
# esl_service.py — play()
async def play(self, audio_path: str, timeout: float = 60.0):
    # 通过 ESL Inbound Pool 调用 uuid_broadcast
    result = await self.esl_pool.api(
        f"uuid_broadcast {target_uuid} {audio_path} both"
    )
    # 等待估算的播放时长
    await asyncio.sleep(estimated_duration)
```

#### 为什么用 uuid_broadcast 而不是 sendmsg execute？

| 方式 | 问题 |
|------|------|
| `sendmsg execute playback` | Outbound socket 模式下有编解码/媒体路径问题 |
| `uuid_broadcast` | ESL API 直接注入音频到通道，不依赖 socket 控制，更可靠 |

### 3.7 用户音频捕获（ASR 输入）

```python
# call_agent.py — _listen_user()
async def _listen_user(self, ...):
    # 1. 获取音频队列（dialplan 中 audio_stream 已持续推送）
    audio_queue = await self.session.start_audio_capture()

    # 2. 清空 TTS 播放期间积累的残留帧
    while not audio_queue.empty():
        audio_queue.get_nowait()

    # 3. 通过 AudioStreamAdapter + VAD 检测用户说话
    adapter = AudioStreamAdapter(audio_queue, vad_silence_ms=500, ...)

    # 4. ASR 流式识别
    async for result in self._asr_with_retry(adapter.stream()):
        if result.is_final and result.text:
            return result.text
```

```python
# esl_service.py — start_audio_capture()
async def start_audio_capture(self) -> asyncio.Queue:
    # dialplan 中 audio_stream 已启动，直接获取全局队列
    if self.ws_server:
        ws_queue = await self.ws_server.get_session_queue(self._uuid, timeout=5.0)
        if ws_queue:
            self._audio_mode = "websocket"
            return ws_queue

    # 降级：文件轮询
    ...
```

#### 音频路径

```
用户麦克风 (Zoiper)
    │ RTP
    ▼
FreeSWITCH sofia channel
    │ mod_audio_stream (read 方向)
    ▼
ws://backend:8765/{uuid}
    │
    ▼
AudioStreamWebSocket._global_queue
    │
    ▼
AudioStreamAdapter → VAD 检测 → ASR 识别 → LLM → TTS
```

### 3.8 TTS 与 ASR 的音频隔离

关键设计：`mod_audio_stream` 的 `read` 方向只捕获用户麦克风音频，不混入 TTS 播放音频。

```
sofia channel 音频流:
┌─────────────────────────────────────┐
│  write 方向: TTS 播放 → RTP → 用户  │
│  read  方向: 用户麦克风 → RTP → FS  │  ← mod_audio_stream 只捕获这个
└─────────────────────────────────────┘
```

这避免了 ASR 识别到自己播放的 TTS 音频（回声问题）。

---

## 四、完整外呼流程图

```
[1] REST API 发起外呼
    POST /api/call → backend/api/main.py

[2] ESL Inbound Pool 发起 originate
    ESL :8021 → bgapi originate [ai_agent=true,...] user/1001@domain

[3] FreeSWITCH 建立 SIP 呼叫
    INVITE → Zoiper (1001)
    Zoiper 振铃 ✓
    Zoiper 应答 200 OK
    RTP 媒体路径建立

[4] 执行 &bridge(loopback/AI_CALL)
    sofia B-leg ↔ loopback-a ↔ loopback-b

[5] loopback-b 进入 default context
    匹配 ai_call_handler (destination_number=AI_CALL)
    ├─ answer
    ├─ record_session → /recordings/{uuid}.wav
    └─ socket → backend:9999 async full
        └─ FreeSWITCH TCP 连接后端

[5] 后端 ESL Outbound Server 接受连接
    创建 ESLSocketCallSession
    注册到 _active_calls[uuid] → CallAgent

[6] CallAgent.run() 启动
    ├─ session.connect() → ESL 握手，读取 channel 变量
    ├─ 查询 CRM 客户信息
    ├─ 构建 System Prompt + 话术配置
    ├─ 启动事件监听 (read_events)
    └─ _conversation_loop()

[7] 对话循环
    ┌─ _say_opening()
    │   └─ TTS 合成 → 写 WAV → uuid_broadcast → 用户听到开场白
    │
    └─ while 通话中:
        ├─ _listen_user()
        │   ├─ start_audio_capture() → 获取 audio_queue
        │   ├─ 清空残留帧
        │   ├─ VAD 检测用户说话
        │   └─ ASR 流式识别 → 用户文本
        │
        ├─ _think_and_reply()
        │   └─ LLM 生成回复 + 动作
        │
        └─ _say(reply_text)
            └─ TTS → 写 WAV → uuid_broadcast → 用户听到回复
```

---

## 五、配置检查清单

### FreeSWITCH 端

| 配置项 | 文件 | 值 | 必须 |
|--------|------|-----|------|
| mod_audio_stream 加载 | `modules.conf.xml` | `<load module="mod_audio_stream"/>` | 是 |
| audio_stream WebSocket | `audio_stream.conf.xml` | `ws://127.0.0.1:8765` | 是 |
| audio_stream 采样率 | `audio_stream.conf.xml` | `8000` | 与 ASR 一致 |
| audio_stream 方向 | `audio_stream.conf.xml` | `read` | 避免回声 |
| sofia internal context | `sofia.conf.xml` | `context=internal` | 是 |
| AI_Agent_Call 扩展 | `internal.xml` | `audio_stream` + `socket` | 是 |
| backend_socket_host | `vars.xml` | `backend` | 是 |
| backend_socket_port | `vars.xml` | `9999` | 是 |

### 后端端

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| FreeSWITCH 主机 | `FS_HOST` | `freeswitch` | ESL 连接目标 |
| ESL Inbound 端口 | `FS_ESL_PORT` | `8021` | Inbound API |
| ESL Outbound 端口 | `FS_SOCKET_PORT` | `9999` | 接受 FS 连入 |
| AudioStream 端口 | `FS_AUDIO_STREAM_PORT` | `8765` | 接收 PCM 音频 |
| ASR 采样率 | `ASR_SAMPLE_RATE` | `8000` | 与 audio_stream 一致 |
| VAD 静音阈值 | `VAD_SILENCE_MS` | `500` | 用户说完的判定停顿 |

---

## 六、常见问题

### Q1: TTS 播放成功但 ASR 识别不到用户说话

**可能原因**:
- `mod_audio_stream` 方向设为 `mixed` 或 `write`，没有捕获到用户麦克风
- audio_stream WebSocket 未连接（网络不通或端口未开放）
- ASR 采样率与 audio_stream 不一致

**排查**:
```bash
# 检查 audio_stream WebSocket 连接
docker-compose logs backend | grep "audio_stream.*连接"

# 检查音频帧接收
docker-compose logs backend | grep "收到音频帧"

# 检查 FreeSWITCH dialplan 匹配
docker-compose logs freeswitch | grep "AI_Agent_Call"
```

### Q2: originate 成功但 B-leg 未进入 AI 扩展

**可能原因**:
- `ai_agent` 变量未正确传递到 B-leg
- B-leg 进入了错误的 dialplan context

**排查**:
```bash
# 检查 B-leg 的 channel 变量
fs_cli -x "uuid_dump {b-leg-uuid} | grep ai_agent"

# 确认 dialplan 匹配
docker-compose logs freeswitch | grep "AI_Agent_Call"
```

### Q3: audio_stream 只收到 1 帧就断开

**原因**: loopback bridge 架构下，`mod_audio_stream` 附加到 loopback 通道时，
TTS 播放期间有音频，但 TTS 结束后用户麦克风音频不再流经 loopback 通道。

**架构限制**:
```
用户麦克风 → sofia B-leg → loopback-a → loopback-b(socket) → 后端
```
loopback 通道只接收从后端发送的 TTS 音频（write 方向），
用户麦克风音频只流经 sofia B-leg，不流经 loopback 通道。

**替代方案探索中**: 需要找到能在非 bridged 通道上捕获音频的方法。
