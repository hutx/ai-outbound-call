# mod_audio_fork 完整文档

## 概述

`mod_audio_fork` 是 FreeSWITCH 的一个核心模块，用于**实时音频流复制和转发**。它允许在通话过程中将音频流同时发送到多个目的地，是实现实时语音识别（ASR）、录音、监控等功能的关键模块。

### 为什么需要 mod_audio_fork？

在 AI 语音交互场景中，传统的 IVR（"请按 1...请按 2..."）体验已经无法满足需求。现代 AI 语音助手需要做到：
- **毫秒级响应** - 端到端延迟控制在 500ms 以内
- **全双工对话** - 支持打断（Barge-in），用户说话时机器人立刻闭嘴
- **流式处理** - ASR 边听边转、LLM 边想边吐字、TTS 边收字边合成

`mod_audio_fork` 通过 WebSocket 将音频流实时推送到 AI 服务，是连接电信世界（FreeSWITCH/RTP）和互联网世界（Python/Node.js/AI 服务）的桥梁。

### 典型架构

```
┌─────────────┐    WebSocket    ┌─────────────────────────────────┐
│             │ ◄─────────────► │         AI Gateway              │
│ FreeSWITCH  │   mod_audio_fork │  ┌─────┐  ┌─────┐  ┌─────┐    │
│   (RTP)     │                 │  │ ASR │→ │ LLM │→ │ TTS │    │
│             │                 │  └─────┘  └─────  └─────┘    │
└─────────────┘                 │         (Streaming)             │
                                └─────────────────────────────────┘
```

---

## 核心功能

### 1. 音频流复制（Forking）
- 将通话中的音频流复制到外部服务
- 支持双向音频（inbound/outbound）或单向
- 低延迟，适合实时处理场景

### 2. 支持的协议
- **WebSocket** - 最常用的方式，适合实时 ASR/TTS
- **HTTP POST** - 分块发送音频数据
- **TCP/UDP** - 原始 socket 连接
- **File** - 本地文件录制

### 3. 音频格式
- 支持多种采样率：8000Hz, 16000Hz, 48000Hz 等
- 编码格式：PCM (SLIN), μ-law, A-law
- 可配置音频包大小（packet size）

---

## 全链路流式处理架构

### 延迟组成分析

一个典型的 AI 语音对话流程：

```
1. 用户说话 → 电话网 → 软交换
2. 软交换 → 媒体服务（VAD 切片）
3. ASR → 转文字
4. LLM → 生成回复
5. TTS → 转语音
6. 软交换 → 电话网 → 用户听到
```

**串行处理的延迟** = ASR 耗时 + LLM 耗时 + TTS 耗时 + 网络传输 ≥ 3 秒（无法接受）

### 流式处理方案

要做到毫秒级响应，必须全链路流式（Streaming）：

| 环节 | 传统方式 | 流式方式 |
|------|----------|----------|
| ASR | 等用户说完再识别 | 边听边转，实时输出 partial 结果 |
| LLM | 等完整 prompt 再回答 | 边想边吐字（token streaming） |
| TTS | 等完整文本再合成 | 边收字边合成音频 |
| 播放 | 等音频合成完再播放 | 边收音频边播放（Jitter Buffer） |

### 关键优化点

1. **抛弃 MRCP，拥抱 WebSocket** - MRCP 太重、太慢、太贵
2. **全双工处理** - 同时处理听和说，支持打断
3. **Jitter Buffer** - 解决网络抖动导致的卡顿
4. **Barge-in 打断** - VAD 检测用户说话时立刻停止 TTS

---

## 安装与编译

### 检查模块是否已加载

```bash
# 在 FreeSWITCH CLI 中
fs_cli> module_exists mod_audio_fork
# 或
fs_cli> status module mod_audio_fork
```

### 编译安装

```bash
cd /usr/src/freeswitch
./configure
make mod_audio_fork-install
```

### 加载模块

```bash
# 在 FreeSWITCH CLI 中
fs_cli> load mod_audio_fork

# 或添加到 autoload.xml 中自动加载
# /usr/local/freeswitch/conf/autoload_configs/autoload.xml
```

---

## 基本用法

### Dialplan 中使用

```xml
<extension name="audio_fork_example">
    <condition field="destination_number" expression="^1234$">
        <!-- 启动音频复制 -->
        <action application="audio_fork" 
                data="ws://localhost:8080/audio"/>
        
        <!-- 后续操作 -->
        <action application="answer"/>
        <action application="sleep" data="1000"/>
        <action application="playback" data="/path/to/welcome.wav"/>
        
        <!-- 停止音频复制 -->
        <action application="audio_fork" data="stop"/>
        
        <action application="hangup"/>
    </condition>
</extension>
```

### Lua 脚本中使用

```lua
-- 启动音频复制
session:execute("audio_fork", "ws://localhost:8080/audio")

-- 执行其他操作
session:answer()
session:streamFile("/path/to/audio.wav")

-- 停止音频复制
session:execute("audio_fork", "stop")
```

### ESL (Event Socket Library)

```python
from freeswitch.fs import ESL

# 连接到 FreeSWITCH
esl = ESL('fs://user:pass@localhost:8021')

# 发送音频复制命令
esl.send('uuid_audio_fork <call_uuid> start ws://localhost:8080/audio')

# 停止
esl.send('uuid_audio_fork <call_uuid> stop')
```

---

## 高级配置参数

### WebSocket 模式参数

```xml
<action application="audio_fork" 
      data="ws://localhost:8080/audio 
            method=POST 
            content-type=audio/raw 
            sample-rate=16000 
            codec=SLIN 
            channels=1 
            buffer-size=20"/>
```

### 参数详解

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `method` | POST | HTTP 方法 (GET/POST) |
| `content-type` | audio/raw | Content-Type 头 |
| `sample-rate` | 8000 | 采样率 (8000/16000/48000) |
| `codec` | SLIN | 音频编码 (SLIN/PCMU/PCMA) |
| `channels` | 1 | 声道数 (1=单声道，2=立体声) |
| `buffer-size` | 20 | 缓冲区大小（毫秒） |
| `bidirectional` | false | 是否双向传输 |
| `full-duplex` | false | 全双工模式 |

### 方向控制

```xml
<!-- 仅发送接收到的音频 (inbound) -->
<action application="audio_fork" 
      data="ws://localhost:8080/audio direction=inbound"/>

<!-- 仅发送播放的音频 (outbound) -->
<action application="audio_fork" 
      data="ws://localhost:8080/audio direction=outbound"/>

<!-- 双向音频 (默认) -->
<action application="audio_fork" 
      data="ws://localhost:8080/audio direction=both"/>
```

---

## Python 完整实现：AI Gateway

以下是生产级别的 Python AI Gateway 核心代码，展示如何与 `mod_audio_fork` 配合实现低延迟语音交互。

### 1. ESL 控制信令（启动音频旁路）

```python
import asyncio
from eventsocket import EventSocket

async def start_audio_fork(uuid: str):
    """
    连接 FreeSWITCH ESL，命令其将指定 UUID 的音频流推送到 WebSocket Server
    """
    esl_host = "127.0.0.1"
    esl_port = 8021
    esl_pass = "ClueCon"
    
    # WebSocket 地址，FreeSWITCH 将作为客户端连接此地址
    ws_url = f"ws://127.0.0.1:8000/media/{uuid}"
    
    try:
        conn = EventSocket(esl_host, esl_port, esl_pass)
        await conn.connect()
        
        # 关键命令：mod_audio_fork 或类似模块
        # 这里的参数 mono 表示单声道，8000 是采样率
        cmd = f"uuid_audio_fork {uuid} start {ws_url} mono 8000"
        response = await conn.api(cmd)
        print(f"[{uuid}] Fork command sent. Response: {response}")
        
        await conn.disconnect()
    except Exception as e:
        print(f"[{uuid}] ESL Error: {e}")

# 执行后，FreeSWITCH 会发起 WebSocket 连接并开始发送 8k PCM 音频数据
```

### 2. WebSocket 服务端入口（接收音频流）

```python
import asyncio
import json
import websockets
from websockets.server import serve

async def media_handler(websocket):
    """
    处理 FreeSWITCH 推送过来的 WebSocket 连接
    path 格式通常为 /media/{uuid}
    """
    path = websocket.request.path
    uuid = path.split('/')[-1]
    print(f"[{uuid}] Connection established")
    
    try:
        async for message in websocket:
            if isinstance(message, str):
                # 处理 JSON 元数据 (Metadata)
                metadata = json.loads(message)
                print(f"[{uuid}] Metadata received: {metadata.get('event')}")
            elif isinstance(message, bytes):
                # 处理二进制音频流 (PCM Raw Data)
                # 这里必须是异步非阻塞的，不能卡住 Event Loop
                asyncio.create_task(process_audio_packet(uuid, message, websocket))
    except websockets.exceptions.ConnectionClosed:
        print(f"[{uuid}] Connection closed")
    except Exception as e:
        print(f"[{uuid}] Error: {e}")

async def process_audio_packet(uuid, audio_data, ws_conn):
    # 下一步：送入 VAD 和 ASR
    pass

async def main():
    async with serve(media_handler, "0.0.0.0", 8000):
        print("AI Gateway Audio Server running on :8000")
        await asyncio.Future()  # run forever

# 启动后，控制台会显示 "AI Gateway Audio Server running on :8000"
# 当电话接通，会打印 "Connection established"，随后不断接收 bytes 数据
```

### 3. 流式 ASR 处理（Mock 示例）

```python
async def stream_asr_processor(audio_queue: asyncio.Queue, text_queue: asyncio.Queue):
    """
    模拟 ASR 引擎：从音频队列取数据，实时吐出文本
    """
    buffer = b""
    
    while True:
        chunk = await audio_queue.get()
        if chunk is None:
            break  # 结束信号
        
        buffer += chunk
        
        # 模拟：每积累一定数据量（例如 200ms）进行一次识别
        if len(buffer) > 3200:
            # 真实场景这里会调用 Google STT / Azure / FunASR 的流式 API
            # 这里模拟识别结果
            recognized_text = "你好"
            print(f"ASR Partial: {recognized_text}")
            await text_queue.put(recognized_text)
            buffer = b""  # 清空缓冲区

# 这是一个消费者协程。随着音频包不断入队，控制台会打印 "ASR Partial: 你好"
# 这种设计解耦了网络接收和 ASR 处理
```

### 4. LLM 流式生成器（降低首字延迟 TTFB）

```python
async def llm_stream_generator(prompt: str):
    """
    模拟 LLM (如 GPT-4) 的流式输出
    """
    # 模拟网络延迟
    await asyncio.sleep(0.2)
    
    response_text = "我是，AtomsCat，你的，AI，架构师。"
    tokens = response_text.split(',')
    
    for token in tokens:
        # 模拟 Token 生成间隔
        await asyncio.sleep(0.05)
        yield token

async def logic_processor(text_queue: asyncio.Queue, tts_queue: asyncio.Queue):
    while True:
        text = await text_queue.get()
        # 拿到 ASR 的文本，构建 Prompt
        prompt = f"User said: {text}"
        print(f"LLM Thinking for: {text}")
        
        async for token in llm_stream_generator(prompt):
            print(f"LLM Token: {token}")
            # 立即送入 TTS 队列，不要等整句结束！
            await tts_queue.put(token)

# 控制台会依次快速打印：
# LLM Token: 我是
# LLM Token: AtomsCat
# ...
# 这种机制确保 TTS 可以在 LLM 还没想好下一句时就开始合成第一句
```

### 5. Jitter Buffer（抖动缓冲区）实现

```python
from collections import deque

class JitterBuffer:
    """
    简易 Jitter Buffer 实现
    解决网络不稳定导致的音频卡顿问题
    """
    def __init__(self, prefetch_count=5):
        self.queue = deque()
        self.prefetch_count = prefetch_count
        self.is_buffering = True
    
    def write(self, packet: bytes):
        self.queue.append(packet)
        # 当缓冲区积累到一定程度，标记为可播放
        if self.is_buffering and len(self.queue) >= self.prefetch_count:
            self.is_buffering = False
    
    def read(self) -> bytes:
        if self.is_buffering:
            return b'\x00' * 160  # 返回静音帧 (Silence)
        
        if len(self.queue) > 0:
            return self.queue.popleft()
        else:
            # 缓冲区耗尽 (Underrun)，进入缓冲状态
            self.is_buffering = True
            return b'\x00' * 160  # 返回静音帧

# 这个类管理音频包的读写
# write() 负责存，read() 负责取
# 关键在于 handle Underrun：如果没数据了，不要报错，而是发送静音帧，并重新进入缓冲模式
```

### 6. Barge-in 打断实现（核心难点）

```python
class CallSession:
    def __init__(self, uuid, websocket):
        self.uuid = uuid
        self.websocket = websocket
        self.is_playing = False
        self.tts_task = None
    
    async def on_vad_detected(self):
        """
        当检测到用户说话时调用
        实现打断功能：机器人立刻闭嘴
        """
        if self.is_playing:
            print(f"[{self.uuid}] Barge-in! Stopping TTS.")
            
            # 1. 取消当前的 TTS 生成任务
            if self.tts_task:
                self.tts_task.cancel()
            
            # 2. 发送指令给 FreeSWITCH 清空其内部 Buffer (非常关键！)
            # FreeSWITCH 通常支持通过 WS 发送 json 格式的控制命令
            # 或者通过 ESL 发送 'uuid_break'
            clear_cmd = {
                "event": "clear_audio_buffer"
            }
            await self.websocket.send(json.dumps(clear_cmd))
            self.is_playing = False
    
    async def send_audio(self, audio_stream):
        self.is_playing = True
        self.tts_task = asyncio.current_task()
        
        try:
            async for chunk in audio_stream:
                await self.websocket.send(chunk)
                await asyncio.sleep(0.02)  # 模拟实时发送节奏
        except asyncio.CancelledError:
            print(f"[{self.uuid}] Audio sending cancelled")
        finally:
            self.is_playing = False

# 当 on_vad_detected 被触发时，正在进行的 send_audio 协程会被 cancel() 抛出异常并停止
# 同时向 FS 发送清空指令，防止用户听到"残留"的半句话
```

---

## 性能优化与踩坑经验

### 踩坑经验：字节序（Endianness）问题

**问题**：很多开发者在做 Python 和 C (FreeSWITCH) 交互时会遇到声音全是杂音的情况。

**原因**：FreeSWITCH 吐出来的 PCM 通常是 **Little Endian** (小端序)，而某些 Python 库处理音频默认 **Big Endian**。

**解决方案**：
```python
import struct

# 将 bytes 转为 short (int16) 数组，使用小端序 '<'
samples = struct.unpack(f'<{len(data)//2}h', data)
```

---

### 编码格式选择：G.711 vs Opus vs L16

| 编码 | 带宽 | CPU | 适用场景 |
|------|------|-----|----------|
| G.711 (PCMU/PCMA) | 64kbps | 低 | 老系统兼容 |
| L16 (Raw PCM) | 128kbps (16kHz) | 最低 | 内网、处理最快 |
| Opus | 16-64kbps | 中 | 公网传输、抗丢包 |

**建议**：
- 服务器内部（Loopback 或局域网）：使用 **L16**，无损且处理最快
- 跨公网传输：使用 **Opus**，抗丢包能力强

---

### TCP vs UDP 争议

**问题**：WebSocket 基于 TCP，传实时语音会不会因为丢包重传导致延迟累积？

**架构师视角**：
- 在服务器内部（Loopback 或局域网），TCP 的开销和延迟可以忽略不计
- WebSocket 的便利性远大于 UDP 带来的那一点点性能提升
- FreeSWITCH 实际上充当了 **TCP (WS) 转 UDP (RTP)** 的网关角色
- 公网传输（FS 到用户手机）仍然走 UDP (RTP)

---

## 性能瓶颈分析

### 主要瓶颈

1. **ASR 识别速度** - 目前最大的瓶颈
   - ❌ 不要用 HTTP REST API
   - ✅ 必须用 gRPC 或 WebSocket 长连接流式接口

2. **GC 停顿** - Python 的 GC 机制在高并发下可能导致音频流短暂卡顿
   - ✅ 生产环境建议使用 **uvloop** 替代标准 asyncio loop

3. **VAD 误判** - 环境噪音会导致 VAD 无法截断，导致机器人一直不说话
   - ✅ 需要引入降噪模块（如 RNNoise）

### 监控指标（必须记录）

- **首字节延迟（TTFB）** - 任何超过 500ms 的请求都值得报警
- 端到端延迟（用户说完到机器人开始回复）
- ASR 识别延迟
- LLM 首 token 延迟
- TTS 合成延迟
- Jitter Buffer 填充率

---

## 拿来即用的结论（Takeaway）

1. ✅ **抛弃 MRCP，拥抱 WebSocket + FreeSWITCH mod_audio_fork**
2. ✅ **全链路流式** - ASR、LLM、TTS 必须全部 Streaming 化，任何一个环节 block 都会导致延迟爆炸
3. ✅ **Barge-in 是灵魂** - 没有打断功能的 AI 语音就是智障。必须实现服务端 VAD + Buffer 清空
4. ✅ **监控 TTFB** - 必须记录每一段音频的"首字节延迟"，任何超过 500ms 的请求都值得报警
5. ✅ **使用 uvloop** - 生产环境替换标准 asyncio loop，减少 GC 停顿

---

## 版本兼容性（来自社区反馈）

根据社区实际使用反馈：

| 组件 | 推荐版本 | 说明 |
|------|----------|------|
| FreeSWITCH | 1.10.12 | 稳定版本 |
| libwebsocket | 4.3.2 | 可正常调通 |

**参考实现**：
- https://github.com/mdslaney/drachtio-freeswitch-modules/tree/main/modules/mod_audio_fork

---

## 实际应用场景

### 场景 1: 实时语音识别 (ASR)

```xml
<extension name="asr_integration">
    <condition field="destination_number" expression="^5000$">
        <!-- 启动音频复制到 ASR 服务 -->
        <action application="audio_fork" 
                data="ws://asr-service:8765/live 
                      sample-rate=16000 
                      codec=SLIN"/>
        
        <action application="answer"/>
        <action application="playback" data="ivr-greeting.wav"/>
        
        <!-- 等待 ASR 处理完成，通过事件或 API 控制 -->
        <action application="sleep" data="5000"/>
        
        <!-- 停止音频复制 -->
        <action application="audio_fork" data="stop"/>
        <action application="hangup"/>
    </condition>
</extension>
```

### 场景 2: 通话录音 + 实时分析

```xml
<extension name="record_and_analyze">
    <condition field="destination_number" expression="^9.*$">
        <!-- 同时发送到录音服务和 ASR 服务 -->
        <action application="audio_fork" 
                data="ws://recorder:8080/record"/>
        <action application="audio_fork" 
                data="ws://analyzer:8081/analyze"/>
        
        <action application="answer"/>
        <action application="record" data="/recordings/${uuid}.wav"/>
        <action application="bridge" data="user/${destination_number}"/>
        
        <action application="audio_fork" data="stop"/>
    </condition>
</extension>
```

### 场景 3: 智能外呼系统 (AI 对话)

```xml
<extension name="ai_outbound">
    <condition field="destination_number" expression="^ai_.*$">
        <!-- 连接到 AI 对话服务 -->
        <action application="audio_fork" 
                data="ws://ai-service:8765/session?call_uuid=${uuid}
                      sample-rate=16000
                      codec=SLIN
                      bidirectional=true"/>
        
        <action application="answer"/>
        
        <!-- AI 服务会处理整个对话流程 -->
        <!-- 通过 WebSocket 接收 TTS 音频并播放 -->
        
        <!-- 等待对话结束 (由 AI 服务控制) -->
        <action application="sleep" data="300000"/>
        
        <action application="audio_fork" data="stop"/>
        <action application="hangup"/>
    </condition>
</extension>
```

---

## 调试与故障排查

### 启用调试日志

```xml
<!-- 在 freeswitch.xml 或 audio_fork.xml 中 -->
<configuration name="audio_fork.conf" description="Audio Fork Configuration">
    <settings>
        <param name="debug-level" value="7"/>
        <param name="log-audio-packets" value="true"/>
    </settings>
</configuration>
```

### 常见问题

#### 1. 模块加载失败
```bash
# 检查模块是否存在
ls -la /usr/local/freeswitch/mod/mod_audio_fork.so

# 重新编译
cd /usr/src/freeswitch/libs/mod_audio_fork
make clean && make install
```

#### 2. WebSocket 连接失败
```bash
# 检查端口是否监听
netstat -tlnp | grep 8765

# 检查防火墙
iptables -L -n | grep 8765

# 测试 WebSocket 连接
wscat -c ws://localhost:8765/audio
```

#### 3. 音频质量问题
- 检查采样率匹配 (FreeSWITCH 和服务器必须一致)
- 检查编码格式 (SLIN/PCMU/PCMA)
- 增加缓冲区大小减少丢包
- 检查网络延迟

#### 4. 内存泄漏
```bash
# 监控 FreeSWITCH 内存
top -p $(pgrep -f freeswitch)

# 检查活跃的 audio_fork 会话
fs_cli> show calls
fs_cli> status
```

---

## API 参考

### CLI 命令

```bash
# 查看所有活跃的音频复制会话
fs_cli> audio_fork status

# 停止指定呼叫的音频复制
fs_cli> audio_fork stop <call_uuid>

# 重启指定呼叫的音频复制
fs_cli> audio_fork restart <call_uuid> <new_url>
```

### ESL API

```bash
# 开始音频复制
uuid_audio_fork <uuid> start <url> [params]

# 停止音频复制
uuid_audio_fork <uuid> stop

# 查询状态
uuid_audio_fork <uuid> status
```

### 事件订阅

```python
# 订阅 audio_fork 相关事件
esl.send('event json CUSTOM audio_fork::*')

# 监听的事件类型:
# - audio_fork::start
# - audio_fork::stop
# - audio_fork::error
```

---

## 安全注意事项

### 1. 认证与授权
```xml
<!-- 使用带 token 的 WebSocket URL -->
<action application="audio_fork" 
      data="ws://user:token@localhost:8765/audio"/>
```

### 2. 加密传输
```xml
<!-- 使用 WSS (WebSocket Secure) -->
<action application="audio_fork" 
      data="wss://secure-server:8765/audio"/>
```

### 3. 访问控制
```python
# WebSocket 服务端验证
async def verify_client(path, headers):
    token = headers.get('Authorization', '').replace('Bearer ', '')
    if not is_valid_token(token):
        return False
    return True
```

### 4. 速率限制
```python
# 限制每个 IP 的连接数
from collections import defaultdict
connection_counts = defaultdict(int)

async def rate_limit(ip):
    if connection_counts[ip] > MAX_CONNECTIONS:
        return False
    connection_counts[ip] += 1
    return True
```

---

## 最佳实践

### ✅ 推荐做法
1. 在生产环境使用 WSS 加密
2. 实现重连机制和心跳检测
3. 记录所有音频复制会话的日志
4. 设置合理的超时和重试策略
5. 监控资源使用情况

### ❌ 避免做法
1. 不要在高延迟网络上使用
2. 不要无限制地开启 audio_fork 会话
3. 不要在音频流中传输敏感信息（除非加密）
4. 不要忽略错误处理和日志记录
5. 不要使用过大的缓冲区（会增加延迟）

---

## 相关资源

- **官方文档**: https://freeswitch.org/confluence/display/FREESWITCH/mod_audio_fork
- **源码**: https://github.com/freeswitch/freeswitch/tree/master/src/mod/applications/mod_audio_fork
- **Drachtio 实现**: https://github.com/mdslaney/drachtio-freeswitch-modules/tree/main/modules/mod_audio_fork
- **SignalWire 文档**: https://signalwire.com/docs/freeswitch
- **FreeSWITCH 社区**: https://freeswitch.org/support
- **知乎实战文章**: 毫秒级响应挑战：基于 WebSocket 与软交换构建实时 AI 语音助手全链路优化

---

## 版本历史

| FreeSWITCH 版本 | mod_audio_fork 特性 |
|----------------|---------------------|
| 1.6.x | 基础 WebSocket 支持 |
| 1.8.x | 添加双向音频支持 |
| 1.10.x | 性能优化，bug 修复 |
| 1.12.x | 增强错误处理 |

---

*文档最后更新：2026-04-15*
