# FreeSWITCH 实现 AI 电话机器人——整体实现方案（二）

**作者：** 了了明明  
**更新时间：** 2026-04-04

---

## 摘要

本文介绍了一种基于 **FreeSWITCH** 的电话机器人系统实现方案，C++ 实现底层数据流转发和管理，上层业务由 Python 实现，方便对接各种模型和业务逻辑实现。该系统通过 **mod_forkstream** 模块实现音频流的输入输出和转发，使用 **WebSocket** 服务端作为中间桥接层，**Python 客户端** 实现灵活的对话逻辑管理。系统集成了 **ASR**（语音识别）、**TTS**（语音合成）和 **LLM**（大语言模型）服务，支持问卷调查、智能客服等多种应用场景。

**关键词：** FreeSWITCH、电话机器人、WebSocket、ASR、TTS、LLM、Python

---

## 1. 引言

随着人工智能技术的发展，电话机器人被广泛应用于客户服务、问卷调查、预约提醒等场景。传统的电话机器人实现方案往往存在音频流处理复杂、扩展性差、对话逻辑固化等问题。本文提出的系统采用模块化设计，通过分离音频流处理、命令解析和对话逻辑，实现了高可扩展、易维护的电话机器人架构。

### 1.1 系统特点

- **音频流模块化处理：** 通过 mod_forkstream 模块实现音频流的 fork 和转发
- **WebSocket 中间桥接：** C++ 实现的 WebSocket 服务端作为 FreeSWITCH 与 Python 客户端的桥梁
- **灵活的对话管理：** Python 实现的对话管理器支持自定义问卷流程和自由对话
- **多服务集成：** 支持 ASR、TTS、LLM 等 AI 服务的配置和调用
- **低延迟实时通信：** 基于 WebSocket 的实时双向通信，支持音频流和文本消息

### 1.2 文档组织

本文档共分为 7 章：

1. 第 1 章：引言，介绍系统背景和特点
2. 第 2 章：系统架构，总体设计和各模块职责
3. 第 3 章：mod_forkstream 模块设计，FreeSWITCH 音频流扩展
4. 第 4 章：WebSocket 服务端，中间桥接层实现
5. 第 5 章：Python 客户端，对话逻辑和 AI 服务集成
6. 第 6 章：部署与使用，编译安装和运行示例

---

## 2. 系统架构

### 2.1 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         电话用户                                │
└──────────────────────────────┬──────────────────────────────────┘
                               │ PSTN/VoIP
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                        FreeSWITCH                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              mod_forkstream (音频流模块)                   │   │
│  │  ┌──────────────┐          ┌──────────────┐              │   │
│  │  │   输入流     │          │   输出流     │              │   │
│  │  │ (ASR/录音)   │          │ (TTS/播放)   │              │   │
│  │  └──────┬───────┘          └──────┬───────┘              │   │
│  └─────────┼─────────────────────────┼───────────────────────┘   │
│            │      WebSocket (音频流 + 控制命令)                   │
└────────────┼────────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   WebSocket 服务端 (C++)                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                     连接管理                              │   │
│  │  ┌──────────────┐          ┌──────────────┐              │   │
│  │  │ FreeSwitch   │          │ Python 客户端 │              │   │
│  │  │  连接池      │          │  连接池      │              │   │
│  │  └──────┬───────┘          └──────┬───────┘              │   │
│  └─────────┼─────────────────────────┼───────────────────────┘   │
│  ┌─────────┴─────────────────────────┴─────────────────────────┐ │
│  │                    数据转发引擎                              │ │
│  │    - 会话管理 - 消息路由 - 音频流中转 - 统计监控             │ │
│  └─────────────────────────────────────────────────────────────┘ │
└────────────┼────────────────────────────────────────────────────┘
             │ WebSocket (控制命令 + 音频流)
             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Python 客户端                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │            对话管理器 (DialogManager)                     │   │
│  │      - 问卷流程 - 状态机 - 意图识别 - 答案管理            │   │
│  └───────────┬──────────────────────────────────────────────┘   │
│  ┌───────────┴──────────────────────────────────────────────┐   │
│  │            管道管理器 (PipelineManager)                   │   │
│  │        - 音频队列 - 流处理 - 数据路由                     │   │
│  └───────────┬──────────────────────────────────────────────┘   │
│  ┌───────────┴──────────────────────────────────────────────┐   │
│  │                  AI 服务集成                               │   │
│  │  ┌────────┐    ┌────────┐    ┌──────────┐                │   │
│  │  │  ASR   │    │  TTS   │    │   LLM    │                │   │
│  │  │Whisper │    │OpenAI  │    │ ChatGPT  │                │   │
│  │  └───┬────┘    └───┬────┘    └────┬─────┘                │   │
│  └──────┼────────────┼──────────────┼────────────────────────┘   │
└─────────┼────────────┼──────────────┼────────────────────────────┘
          │            │              │
          ▼            ▼              ▼
     ┌────────┐   ┌────────┐    ┌──────────┐
     │ ASR    │   │ TTS    │    │ LLM      │
     │ 服务   │   │ 服务   │    │ 服务     │
     └────────┘   └────────┘    └──────────┘
```

### 2.2 各模块职责

| 模块 | 语言 | 职责 |
|------|------|------|
| FreeSWITCH | C | 核心通信平台，处理电话呼叫、媒体编解码 |
| mod_forkstream | C | 音频流扩展，实现输入/输出流的 fork 和转发 |
| WebSocket 服务端 | C++ | 中间桥接层，管理连接、转发数据、解析命令 |
| Python 客户端 | Python | 对话逻辑管理、AI 服务集成、业务实现 |

### 2.3 数据流向

#### 2.3.1 语音识别流程 (ASR)

```
电话用户 → FreeSWITCH → mod_forkstream (输入流)
                        ↓
              WebSocket (二进制音频)
                        ↓
           WebSocket 服务端 → Python 客户端 (PipelineManager)
                        ↓
                     API 调用
                        ↓
                ASR 服务 → 返回文本
                        ↓
                   回调处理
                        ↓
              对话管理器 (DialogManager)
```

#### 2.3.2 语音合成流程 (TTS)

```
对话管理器 (DialogManager)
      ↓
   触发 TTS
      ↓
AI 服务集成 → TTS 服务
      ↓
   返回音频
      ↓
WebSocket 服务端 → mod_forkstream (输出流)
                        ↓
                   播放音频
                        ↓
              FreeSWITCH → 电话用户
```

#### 2.3.3 命令控制流程

```
Python 客户端 → WebSocket 服务端 → FreeSWITCH
                                   - 挂断呼叫
                                   - 转移呼叫
                                   - DTMF 发送
                                   - 变量设置
```

---

## 3. mod_forkstream 模块设计

mod_forkstream 是 FreeSWITCH 的自定义音频流扩展模块，负责音频数据的采集、转发和播放。该模块采用 C 语言编写，通过 FreeSWITCH 的模块加载机制集成到系统中，提供了灵活的音频流处理能力。

### 3.1 模块概述

mod_forkstream 模块的核心功能包括：

- **音频流输入：** 从 FreeSWITCH 媒体引擎捕获音频数据，实时转发到外部服务
- **音频流输出：** 从外部服务接收音频数据，通过 FreeSWITCH 播放给通话对方
- **多数据源支持：** 支持 WebSocket、HTTP、本地文件等多种音频数据来源
- **实时流式传输：** 采用流式传输模式，降低端到端延迟
- **事件驱动架构：** 通过回调机制与 FreeSWITCH 事件系统深度集成

模块采用面向对象设计，通过抽象接口实现数据源的解耦，便于扩展新的数据源类型。

### 3.2 Media Bug 机制

FreeSWITCH 的 `media_bug` 是一个强大的媒体处理机制，它允许模块在不干扰正常通话流程的情况下，访问和操作音频流。

我们通过 `switch_core_media_bug_add` 函数注册了一个 media_bug 回调：

```c
switch_core_media_bug_add(session, "realtimeasr", "realtimeasr", 
                          media_callback, sth, 0, 
                          bug_flags | SMBF_READ_STREAM | SMBF_NO_PAUSE, 
                          &sth->bug)
```

这里的关键参数是 `SMBF_READ_STREAM`，它表示我们要捕获读取流（即对方发来的音频）。通过这个标志，我们可以实时获取通话中的音频数据。

### 3.3 音频数据处理流程

在 media_bug 的回调函数中，我们需要处理不同类型的事件：

```c
static switch_bool_t media_callback(switch_media_bug_t *bug, 
                                     void *user_data, 
                                     switch_abc_type_t type) {
    struct speech_thread_handle *sth = (struct speech_thread_handle *)user_data;
    
    switch (type) {
        case SWITCH_ABC_TYPE_READ:
            // 读取音频数据并转发到 WebSocket 服务
            if (switch_core_media_bug_read(bug, &frame, SWITCH_FALSE) != SWITCH_STATUS_FALSE) {
                asr_feed(sth, frame.data, frame.datalen);
            }
            break;
            
        case SWITCH_ABC_TYPE_WRITE:
            // 处理 TTS 音频数据，播放给用户
            rframe = switch_core_media_bug_get_write_replace_frame(bug);
            // 播放 TTS 音频
            break;
    }
    
    return SWITCH_TRUE;
}
```

### 3.4 音频重采样

在实际应用中，FreeSWITCH 的采样率可能与外部服务要求的采样率不一致。因此，我们需要进行音频重采样：

```c
if (ah->native_rate && ah->samplerate && ah->native_rate != ah->samplerate) {
    if (!ah->resampler) {
        switch_resample_create(&ah->resampler, 
                               ah->samplerate, 
                               ah->native_rate, 
                               (uint32_t)orig_len, 
                               SWITCH_RESAMPLE_QUALITY, 
                               1);
    }
    switch_resample_process(ah->resampler, data, len / 2);
}
```

这段代码会自动创建重采样器，并将音频数据转换为目标采样率。

### 3.5 线程安全设计

在多线程环境下，需要确保共享资源的安全访问。我们使用互斥锁保护共享资源：

```c
switch_mutex_lock(globals.mutex);
session = switch_core_hash_find(globals.session_table, uuid);
switch_mutex_unlock(globals.mutex);
```

### 3.6 使用方式

模块提供了一个名为 `forkzstream` 的 FreeSWITCH 应用，支持以下命令格式：

```
forkzstream <call_uuid> start <reqid> ws <param>
```

**参数说明：**

- `call_uuid`：通话的唯一标识符
- `reqid`：请求的唯一标识符
- `ws`：表示使用 WebSocket 服务
- `param`：JSON 格式的参数，包含 callId、tenantId、botid、fsInstanceId、url、text 等字段

**实际使用示例：**

```
forkzstream 550e8400-e29b-41d4-a716-446655440000 start 123e4567-e89b-12d3-a456-426614174000 ws {"callId":"call_123","tenantId":"tenant_001","botid":"bot_001","fsInstanceId":"fs_001","url":"ws://192.168.1.100:8764"}
```

---

## 4. WebSocket 服务端

WebSocket 服务端是整个系统的中间桥接层，负责管理 FreeSWITCH 和 Python 客户端之间的连接，转发音频流和控制命令。

### 4.1 服务端概述

WebSocket 服务端采用 C++ 实现，使用 websocketpp 库提供 WebSocket 支持。它作为中间层，连接 FreeSWITCH 和 Python 客户端，实现数据的双向转发。

### 4.2 核心功能

- **连接管理：** 维护 FreeSWITCH 连接池和 Python 客户端连接池
- **数据转发：** 在 FreeSWITCH 和 Python 客户端之间转发音频流和文本消息
- **命令解析：** 解析来自 Python 客户端的控制命令，如挂断、转移呼叫等
- **统计监控：** 记录连接数、消息量等统计信息

### 4.3 核心组件

#### 4.3.1 连接信息结构体

```cpp
struct ConnectionInfo {
    std::string connectionId;                    // 连接唯一标识符
    ConnectionType type;                         // 连接类型（FreeSwitch/User）
    std::string sessionId;                       // 关联的会话 ID
    std::chrono::steady_clock::time_point connectedAt;    // 连接时间
    std::chrono::steady_clock::time_point lastActivity;   // 最后活动时间
    uint64_t bytesReceived;                      // 接收字节数
    uint64_t bytesSent;                          // 发送字节数
    uint64_t messagesReceived;                   // 接收消息数
    uint64_t messagesSent;                       // 发送消息数
};
```

#### 4.3.2 会话信息结构体

```cpp
struct SessionInfo {
    std::string sessionId;                       // 会话 ID
    std::string freeSwitchConnectionId;          // FreeSWITCH 连接 ID
    std::string userConnectionId;                // 用户连接 ID
    std::chrono::steady_clock::time_point createdAt;      // 创建时间
    std::map<std::string, std::string> metadata; // 会话元数据
};
```

### 4.4 WebSocket 事件处理

#### 4.4.1 连接建立事件

```cpp
void WebSocketServer::onOpen(websocketpp::connection_hdl hdl) {
    auto con = m_server.get_con_from_hdl(hdl);
    auto query = con->get_uri().get_raw_query();
    
    // 解析查询参数判断连接类型
    ConnectionType type = ConnectionType::Unknown;
    std::string sessionId;
    
    if (query.find("type=freeswitch") != std::string::npos) {
        type = ConnectionType::FreeSwitch;
        auto pos = query.find("session_id=");
        if (pos != std::string::npos) {
            sessionId = query.substr(pos + 11, 36);
        }
    } else if (query.find("type=user") != std::string::npos) {
        type = ConnectionType::User;
    }
    
    // 注册连接
    registerConnection(type, hdl);
    
    // 发送欢迎消息
    json welcome_msg = {
        {"type", "event"},
        {"event", "connected"},
        {"connectionId", info.connectionId},
        {"timestamp", getCurrentTimestamp()}
    };
    
    m_server.send(hdl, welcome_msg.dump(), websocketpp::frame::opcode::text);
}
```

#### 4.4.2 连接关闭事件

```cpp
void WebSocketServer::onClose(websocketpp::connection_hdl hdl) {
    auto con = m_server.get_con_from_hdl(hdl);
    auto code = con->get_local_close_code();
    auto reason = con->get_local_close_reason();
    
    auto& info = m_connections[hdl];
    
    // 如果是 FreeSWITCH 连接，需要注销对应的会话
    if (info.type == ConnectionType::FreeSwitch && !info.sessionId.empty()) {
        unregisterSession(info.sessionId);
    }
    
    // 注销连接
    unregisterConnection(hdl);
}
```

#### 4.4.3 消息接收事件

```cpp
void WebSocketServer::onMessage(websocketpp::connection_hdl hdl, 
                                 Server::message_ptr msg) {
    auto& info = m_connections[hdl];
    info.updateActivity();
    info.messagesReceived++;
    
    // 根据消息类型处理
    if (msg->get_opcode() == websocketpp::frame::opcode::text) {
        // 文本消息：推送到消息队列，由线程池处理
        {
            std::lock_guard<std::mutex> lock(m_messageQueueMutex);
            m_messageQueue.push({msg->get_payload(), hdl});
        }
        m_messageQueueCV.notify_one();
    } else if (msg->get_opcode() == websocketpp::frame::opcode::binary) {
        // 二进制消息：音频数据，直接推送到转发队列
        auto& payload = msg->get_payload();
        pushToDataQueue(info.sessionId, info.type, 
                       reinterpret_cast<const uint8_t*>(payload.data()), 
                       payload.size());
    }
}
```

### 4.5 数据转发机制

数据转发引擎负责在 FreeSWITCH 和 Python 客户端之间转发音频数据：

```cpp
void WebSocketServer::forwardingThreadFunc() {
    while (m_forwardingEnabled.load()) {
        DataPacket packet;
        
        // 从队列获取数据包
        if (popDataPacket(packet)) {
            // 转发到目标类型连接
            ConnectionType targetType = 
                (packet.sourceType == ConnectionType::FreeSwitch) 
                ? ConnectionType::User 
                : ConnectionType::FreeSwitch;
            
            sendToConnectionsBySession(packet.sessionId, targetType, 
                                       packet.data.data(), 
                                       packet.data.size());
            
            // 更新统计
            m_bytesTransferred += packet.data.size();
        }
    }
}
```

### 4.6 通信协议

#### 4.6.1 控制命令消息

```json
{
    "type": "command",
    "action": "start_stream",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "req_id": "req_12345",
    "params": {
        "direction": "both",
        "format": "pcm",
        "sample_rate": 8000,
        "channels": 1
    },
    "timestamp": 1709280000
}
```

#### 4.6.2 事件消息

```json
{
    "type": "event",
    "event": "session_start",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "data": {
        "caller": "+8613800138000",
        "callee": "+8613900139000",
        "duration": 0
    },
    "timestamp": 1709280000
}
```

#### 4.6.3 响应消息

```json
{
    "type": "response",
    "action": "start_stream",
    "status": "success",
    "message": "Stream started successfully",
    "data": {
        "stream_id": "stream_12345"
    },
    "timestamp": 1709280000
}
```

---

## 5. Python 客户端

Python 客户端负责对话逻辑管理和 AI 服务集成，是整个系统的"大脑"。

### 5.1 客户端概述

Python 客户端采用异步编程模型，使用 asyncio 库实现高效的并发处理。它通过 WebSocket 连接到 WebSocket 服务端，接收音频流和控制命令，并调用 AI 服务进行处理。

### 5.2 FreeSwitch 客户端

```python
class FreeSwitchClient:
    """FreeSWITCH WebSocket 客户端"""
    
    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.ws = None
        self.connected = False
        self.loop = asyncio.get_event_loop()
        
        # 回调函数
        self.message_handler = None
        self.audio_handler = None
        self.connect_handler = None
        self.disconnect_handler = None
        self.error_handler = None
    
    async def connect(self):
        """连接到 WebSocket 服务器"""
        uri = self.config.uri
        self.ws = await websockets.connect(uri)
        self.connected = True
        
        if self.connect_handler:
            self.connect_handler()
        
        # 开始接收消息
        asyncio.create_task(self._receive_loop())
    
    async def _receive_loop(self):
        """接收消息循环"""
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    # 文本消息
                    if self.message_handler:
                        self.message_handler(message)
                else:
                    # 二进制消息（音频数据）
                    if self.audio_handler:
                        self.audio_handler(message)
        except Exception as e:
            if self.error_handler:
                self.error_handler(e)
            self.connected = False
            if self.disconnect_handler:
                self.disconnect_handler()
    
    async def send_text(self, message: str):
        """发送文本消息"""
        if self.ws and self.connected:
            await self.ws.send(message)
    
    async def send_audio(self, audio_data: bytes):
        """发送音频数据"""
        if self.ws and self.connected:
            await self.ws.send(audio_data)
    
    async def close(self):
        """关闭连接"""
        if self.ws:
            await self.ws.close()
        self.connected = False
```

### 5.3 OpenAI 服务集成

```python
class OpenAIService:
    """OpenAI 服务集成"""
    
    def __init__(self, api_key: str, base_url: str, model: str = None):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0
        )
    
    async def transcribe(self, audio_data: bytes, 
                        model: str = None, 
                        language: str = "zh") -> str:
        """语音识别（ASR）"""
        model = model or self.model or "whisper-1"
        files = {"file": ("audio.wav", audio_data, "audio/wav")}
        data = {
            "model": model,
            "language": language,
            "response_format": "text"
        }
        response = await self.client.post("/audio/transcriptions", 
                                         files=files, data=data)
        response.raise_for_status()
        return response.text.strip()
    
    async def synthesize(self, text: str, 
                        model: str = None, 
                        voice: str = "alloy", 
                        speed: float = 1.0) -> bytes:
        """语音合成（TTS）"""
        model = model or self.model or "tts-1"
        data = {
            "model": model,
            "input": text,
            "voice": voice,
            "speed": speed,
            "response_format": "mp3"
        }
        response = await self.client.post("/audio/speech", json=data)
        response.raise_for_status()
        return response.content
    
    async def chat(self, messages: List[Dict[str, str]], 
                  model: str = None, 
                  temperature: float = 0.7, 
                  max_tokens: int = 1000) -> str:
        """对话生成（LLM）"""
        model = model or self.model or "gpt-4"
        data = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        response = await self.client.post("/chat/completions", json=data)
        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"]
```

### 5.4 对话管理器

#### 5.4.1 数据结构定义

```python
class DialogState(Enum):
    """对话状态枚举"""
    IDLE = "idle"
    WELCOME = "welcome"
    QUESTION = "question"
    LISTENING = "listening"
    PROCESSING = "processing"
    CONFIRM = "confirm"
    GOODBYE = "goodbye"
    ERROR = "error"

@dataclass
class Question:
    """问卷问题"""
    id: str
    text: str
    options: Optional[List[str]] = None
    confirmation: bool = False
    skip_if_empty: bool = False
    validator: Optional[Callable[[str], bool]] = None
    keywords: Dict[str, str] = field(default_factory=dict)

@dataclass
class Answer:
    """问卷回答"""
    question_id: str
    answer: str
    confidence: float = 1.0
    confirmed: bool = False
    timestamp: str = ""
```

#### 5.4.2 对话管理器实现

```python
class DialogManager:
    """对话管理器"""
    
    def __init__(self, 
                 questions: List[Question],
                 welcome_message: str = "",
                 goodbye_message: str = "",
                 error_message: str = "抱歉，我没有理解您的回答，请再说一次。"):
        self.questions = questions
        self.welcome_message = welcome_message
        self.goodbye_message = goodbye_message
        self.error_message = error_message
        self.context = DialogContext()
        
        # 回调函数
        self.on_speak = None
        self.on_transcript = None
        self.on_state_change = None
        self.on_dialog_complete = None
        
        # 意图识别关键词
        self.intent_keywords = {
            'cancel': ['取消', '停止', '退出', 'cancel', 'stop', 'quit'],
            'repeat': ['重复', '再说', 'repeat', 'say again'],
            'skip': ['跳过', '下一题', 'skip', 'next'],
            'yes': ['是', '对', '是的', 'yes', 'yeah', '对'],
            'no': ['不是', '不对', '否', 'no', 'not']
        }
    
    def start(self):
        """开始对话"""
        self._change_state(DialogState.WELCOME)
        if self.welcome_message:
            self._speak(self.welcome_message)
        self._move_to_question(0)
    
    def handle_input(self, text: str):
        """处理用户输入"""
        text = text.strip()
        if not text:
            return
        
        # 意图识别
        intent = self._detect_intent(text)
        if intent == 'cancel':
            self._finish_dialog()
            return
        elif intent == 'repeat':
            self._repeat_question()
            return
        elif intent == 'skip':
            self._skip_question()
            return
        
        # 处理回答
        self._process_answer(text)
    
    def _process_answer(self, text: str):
        """处理回答"""
        question = self.questions[self.context.current_question_index]
        
        # 关键词映射
        for keyword, mapped_answer in question.keywords.items():
            if keyword in text:
                text = mapped_answer
                break
        
        # 验证回答
        if question.validator and not question.validator(text):
            self._speak(self.error_message)
            return
        
        # 记录回答
        answer = Answer(
            question_id=question.id,
            answer=text,
            timestamp=datetime.now().isoformat()
        )
        self.context.answers.append(answer)
        
        # 是否需要确认
        if question.confirmation:
            self._confirm_answer(answer)
        else:
            self._next_question()
    
    def _confirm_answer(self, answer: Answer):
        """确认回答"""
        self._change_state(DialogState.CONFIRM)
        confirm_text = f"您回答的是{answer.answer}，对吗？"
        self._speak(confirm_text)
    
    def _next_question(self):
        """下一个问题"""
        next_index = self.context.current_question_index + 1
        self._move_to_question(next_index)
    
    def _move_to_question(self, index: int):
        """移动到指定问题"""
        self.context.current_question_index = index
        if index >= len(self.questions):
            self._finish_dialog()
        else:
            self._change_state(DialogState.QUESTION)
            question = self.questions[index]
            self._ask_question(question)
    
    def _ask_question(self, question: Question):
        """提问"""
        self._change_state(DialogState.LISTENING)
        if question.options:
            options_text = ",".join(question.options)
            text = f"{question.text}，选项有：{options_text}"
        else:
            text = question.text
        self._speak(text)
    
    def _finish_dialog(self):
        """结束对话"""
        self._change_state(DialogState.GOODBYE)
        if self.goodbye_message:
            self._speak(self.goodbye_message)
        if self.on_dialog_complete:
            self.on_dialog_complete(self.context.answers)
```

### 5.5 管道管理器

```python
class PipelineManager:
    """管道管理器"""
    
    def __init__(self):
        self.audio_queue = asyncio.Queue(maxsize=100)
        self.handlers = []
    
    def add_handler(self, handler: Callable):
        """添加处理器"""
        self.handlers.append(handler)
    
    async def process_audio(self, audio_data: bytes):
        """处理音频数据"""
        for handler in self.handlers:
            audio_data = await handler(audio_data)
        return audio_data
    
    async def run(self):
        """运行管道"""
        while True:
            audio_data = await self.audio_queue.get()
            result = await self.process_audio(audio_data)
            await self.on_audio_ready(result)
```

---

## 6. 配置 FreeSWITCH 拨号计划

### 6.1 基础配置

编辑 `/usr/local/freeswitch/conf/dialplan/default.xml`：

```xml
<extension name="phonebot_basic">
    <condition field="destination_number" expression="^1234$">
        <action application="answer"/>
        <action application="set" data="session_uuid=${uuid}"/>
        <action application="forkzstream" 
                data="start url=ws://localhost:9002 type=websocket direction=both session_id=${uuid} reqid=tts_${uuid}"/>
        <action application="sleep" data="60000"/>
        <action application="forkzstream" data="stop"/>
    </condition>
</extension>
```

### 6.2 问卷调查应用配置

```xml
<extension name="phonebot_survey">
    <condition field="destination_number" expression="^5678$">
        <action application="answer"/>
        <action application="forkzstream" 
                data="start url=ws://localhost:9002 type=websocket direction=both session_id=${uuid} survey_id=cust_satisfaction_2024"/>
        <action application="set" data="survey_type=customer_feedback"/>
        <action application="set" data="customer_phone=${caller_id_number}"/>
        <action application="sleep" data="300000"/>
        <action application="forkzstream" data="stop"/>
    </condition>
</extension>
```

### 6.3 运行完整系统

#### 6.3.1 启动流程

```bash
# 第 1 步：启动 FreeSWITCH
sudo systemctl start freeswitch

# 第 2 步：启动 WebSocket 服务端
sudo systemctl start freeswitch-ws.service

# 第 3 步：启动 Python 客户端
cd /path/to/freeswitch_bot/python_client
source venv/bin/activate
python examples/example_survey.py

# 第 4 步：测试呼叫
# 使用 SIP 客户端拨打 1234（基础示例）
# 或拨打 5678（问卷调查）
```

### 6.4 示例：完整问卷调查机器人

```python
#!/usr/bin/env python3
"""问卷调查机器人完整示例"""

import asyncio
import logging
import json
from pathlib import Path
from freeswitch_client import FreeSwitchClient, ConnectionConfig, OpenAIConfig
from dialog_manager import DialogManager, Question
from openai_service import OpenAIService

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 定义问卷问题
questions = [
    Question(
        id="satisfaction",
        text="您好，欢迎参加产品满意度调查。您对我们产品的整体满意度如何？",
        options=["非常满意", "满意", "一般", "不满意", "非常不满意"],
        confirmation=True,
        keywords={
            "很满意": "非常满意",
            "挺好": "满意",
            "还行": "一般",
            "不好": "不满意",
            "很差": "非常不满意"
        }
    ),
    Question(
        id="usage_frequency",
        text="您使用我们产品的频率是怎样的？",
        options=["每天使用", "每周几次", "每月几次", "偶尔使用"],
        confirmation=True
    ),
    Question(
        id="duration",
        text="您使用我们产品多长时间了？",
        options=["不到 1 个月", "1-6 个月", "6-12 个月", "1 年以上"],
        confirmation=True
    ),
    Question(
        id="favorite_feature",
        text="您最喜欢我们产品的哪个功能？",
        skip_if_empty=True
    ),
    Question(
        id="improvement",
        text="您认为我们产品有哪些需要改进的地方？",
        skip_if_empty=True
    ),
    Question(
        id="recommend",
        text="您会向朋友推荐我们的产品吗？",
        options=["肯定会", "可能会", "不确定", "不会"],
        confirmation=True
    ),
    Question(
        id="suggestion",
        text="还有其他建议或意见吗？",
        skip_if_empty=True
    )
]

async def main():
    """主函数"""
    # 加载配置
    ws_config = ConnectionConfig(
        uri="ws://localhost:9002",
        reconnect=True,
        reconnect_delay=5.0
    )
    openai_config = OpenAIConfig()
    openai_config.asr_api_key = "sk-your-asr-api-key"
    openai_config.tts_api_key = "sk-your-tts-api-key"
    openai_config.llm_api_key = "sk-your-llm-api-key"
    
    # 创建客户端
    client = FreeSwitchClient(ws_config)
    
    # 初始化 AI 服务
    asr_service = OpenAIService(
        api_key=openai_config.asr_api_key,
        base_url=openai_config.asr_base_url,
        model=openai_config.asr_model
    )
    tts_service = OpenAIService(
        api_key=openai_config.tts_api_key,
        base_url=openai_config.tts_base_url
    )
    
    # 创建对话管理器
    dialog_manager = DialogManager(
        questions=questions,
        welcome_message="您好，欢迎参加产品满意度调查，大约需要 3-5 分钟时间。",
        goodbye_message="感谢您的参与，您的反馈对我们非常重要，再见！",
        error_message="抱歉，我没有理解您的回答，请重新回答。"
    )
    
    # 设置回调
    audio_buffer = bytearray()
    BUFFER_SIZE = 16000  # 1 秒音频
    
    async def on_speak(text: str):
        """朗读回调：TTS 合成并发送"""
        logger.info(f"🔊 朗读：{text}")
        try:
            audio = await tts_service.synthesize(
                text=text,
                voice=openai_config.tts_voice
            )
            await client.send_audio(audio)
        except Exception as e:
            logger.error(f"TTS error: {e}")
    
    async def on_audio(audio_data: bytes):
        """音频处理回调：ASR 识别"""
        audio_buffer.extend(audio_data)
        if len(audio_buffer) >= BUFFER_SIZE:
            audio_to_process = bytes(audio_buffer[:BUFFER_SIZE])
            audio_buffer = audio_buffer[BUFFER_SIZE:]
            try:
                wav_data = _pcm_to_wav(audio_to_process)
                transcript = await asr_service.transcribe(wav_data)
                if transcript and transcript.strip():
                    logger.info(f"🎤 识别：{transcript}")
                    dialog_manager.handle_input(transcript)
            except Exception as e:
                logger.error(f"ASR error: {e}")
    
    def on_state_change(old_state, new_state):
        """状态变化回调"""
        logger.info(f"🔄 状态变化：{old_state} -> {new_state}")
    
    def on_complete(answers):
        """问卷完成回调"""
        logger.info("✅ 问卷完成！")
        print("\n" + "="*50)
        print("问卷调查结果")
        print("="*50)
        for ans in answers:
            print(f" {ans.question_id}: {ans.answer}")
        print("="*50)
        
        # 保存到文件
        results = {
            "answers": [
                {
                    "question_id": ans.question_id,
                    "answer": ans.answer,
                    "confidence": ans.confidence,
                    "confirmed": ans.confirmed
                }
                for ans in answers
            ],
            "completed_at": asyncio.get_event_loop().time()
        }
        output_file = Path("survey_results.json")
        output_file.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        logger.info(f"结果已保存到：{output_file}")
    
    def _pcm_to_wav(pcm_data: bytes) -> bytes:
        """将 PCM 数据转换为 WAV 格式"""
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(8000)
            wav_file.writeframes(pcm_data)
        wav_io.seek(0)
        return wav_io.read()
    
    # 设置回调
    dialog_manager.set_callbacks(
        on_speak=on_speak,
        on_state_change=on_state_change,
        on_dialog_complete=on_complete
    )
    client.set_audio_handler(on_audio)
    
    # 连接并开始对话
    await client.connect()
    dialog_manager.start()
    
    # 保持运行
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("正在关闭...")
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 7. 开源实现

**fork_stream 的开源实现地址：** https://github.com/zhaomingwork/fork_zstream

---

**文档整理完成** ✅
