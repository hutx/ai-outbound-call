# FreeSWITCH 语音路由解决方案

## 项目概述
针对 AI 外呼系统，实现语音在 FreeSWITCH、Zoiper 软电话和后端 AI 服务之间的高效流转。

## 架构流程图

```
┌─────────────────┐    SIP信令    ┌─────────────────┐    RTP媒体流   ┌─────────────────┐
│                 │ ────────────▶ │                 │ ────────────▶ │                 │
│   Zoiper        │               │   FreeSWITCH    │               │    后端 AI      │
│  软电话客户端    │ ◀──────────── │                 │ ◀──────────── │   服务组件      │
│                 │   ESL控制     │                 │   音频流      │                 │
└─────────────────┘               └─────────────────┘               └─────────────────┘
```

## 1. FreeSWITCH 到 Zoiper 的语音转发

### 1.1 内部通话路由（Zoiper ↔ Zoiper）

在 `dialplan/internal.xml` 中配置内部通话路由：

```xml
<extension name="internal_call">
  <condition field="destination_number" expression="^(\d{4})$">
    <action application="export" data="sip_auto_answer=true"/>
    <action application="answer"/>
    <action application="bridge" data="user/$1@${domain_name}"/>
  </condition>
</extension>
```

### 1.2 外呼路由（FreeSWITCH → 运营商 → 被叫）

在 `dialplan/outbound.xml` 中配置外呼路由：

```xml
<extension name="outbound_call">
  <condition field="destination_number" expression="^(\d{11})$">
    <action application="set" data="effective_caller_id_number=$${outbound_caller_id}"/>
    <action application="bridge" data="sofia/gateway/carrier_trunk/$${provider_prefix}$1"/>
  </condition>
</extension>
```

### 1.3 内线转外线（Zoiper → 外部号码）

```xml
<extension name="internal_to_external">
  <condition field="destination_number" expression="^9(\d{11})$">
    <action application="set" data="effective_caller_id_number=${caller_id_number}"/>
    <action application="bridge" data="sofia/gateway/carrier_trunk/$${provider_prefix}$1"/>
  </condition>
</extension>
```

## 2. 语音数据到后端的传输方案

### 2.1 WebSocket 音频流（推荐方案）

FreeSWITCH 使用 `audio_stream` 应用将实时音频推送到后端：

```xml
<extension name="send_audio_to_backend">
  <condition field="${direction}" expression="^outbound$" break="never"/>
  <condition field="${ai_agent}" expression="^true$" break="on-true">
    <action application="answer"/>
    <action application="set" data="media_bug_answer_req=true"/>
    <action application="audio_stream" data="ws://backend:8765 8000 read 20"/>
    <action application="socket" data="backend:9999 async full"/>
  </condition>
</extension>
```

### 2.2 ESL (Event Socket Library) 控制

后端通过 ESL 与 FreeSWITCH 交互：

```python
import asyncio
import socket

class FreeSWITCHClient:
    def __init__(self, host='127.0.0.1', port=8021, password='ClueCon'):
        self.host = host
        self.port = port
        self.password = password
        self.socket = None
        
    async def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))
        
        # 认证流程
        response = self.socket.recv(1024).decode()
        self.socket.send(f"auth {self.password}\n\n".encode())
        
        # 接收认证结果
        response = self.socket.recv(1024).decode()
        if "Reply-Text: +OK accepted" in response:
            print("ESL 认证成功")
        else:
            raise Exception("ESL 认证失败")

    def originate_call(self, callee_number, caller_id="AI_Call"):
        """发起外呼"""
        cmd = f"originate {{origination_caller_id_number={caller_id},ai_agent=true,task_id={self.generate_task_id()}}}sofia/gateway/carrier_trunk/{callee_number} &socket(backend:9999 async full)"
        self.socket.send(f"{cmd}\n\n".encode())

    def generate_task_id(self):
        import uuid
        return str(uuid.uuid4())
```

### 2.3 录音文件存储

同时保存录音文件用于后续处理：

```xml
<action application="set" data="record_file=/recordings/${task_id}/${uuid}.wav"/>
<action application="record_session" data="${record_file}"/>
```

## 3. Zoiper 软电话配置

### 3.1 账户配置
- SIP 服务器：FreeSWITCH 服务器 IP
- 端口：5060
- 用户名：在 `directory/default.xml` 中配置的分机号 (如 1001)
- 密码：对应的密码
- 拨号计划：拨打 9+手机号 进行外呼

### 3.2 音频设置
- 编解码器：优先使用 PCMU/PCMA (G.711)，确保与 FreeSWITCH 一致
- 采样率：8000 Hz
- NAT 穿透：启用 STUN/TURN (如需要)

## 4. 后端语音处理流程

### 4.1 WebSocket 音频接收服务

```python
import asyncio
import websockets
import json
import wave
import io

class AudioReceiver:
    def __init__(self):
        self.active_sessions = {}
        
    async def handle_audio_stream(self, websocket, path):
        """处理来自 FreeSWITCH 的音频流"""
        try:
            async for message in websocket:
                # 解析音频数据
                audio_data = self.parse_audio_frame(message)
                
                # 转发到 ASR 服务
                await self.process_asr(audio_data)
                
                # 如果是 AI 外呼，生成回复并发送到 TTS
                if self.is_ai_call_session(websocket):
                    response = await self.process_llm_response(audio_data)
                    tts_audio = await self.tts_service(response)
                    
                    # 通过 ESL 发送到 FreeSWITCH 播放给被叫
                    await self.play_to_caller(tts_audio)
                    
        except websockets.exceptions.ConnectionClosed:
            print("WebSocket 连接已关闭")
            
    def parse_audio_frame(self, frame):
        """解析音频帧"""
        # 实现音频帧解析逻辑
        return frame
    
    async def process_asr(self, audio_data):
        """处理 ASR 识别"""
        # 调用 ASR 服务
        pass
    
    async def process_llm_response(self, asr_text):
        """处理 LLM 回复"""
        # 调用 LLM 服务
        pass
    
    async def tts_service(self, text):
        """TTS 服务"""
        # 调用 TTS 服务
        pass
    
    async def play_to_caller(self, audio_data):
        """播放音频给被叫"""
        # 通过 ESL 控制 FreeSWITCH 播放音频
        pass
```

### 4.2 ESL 事件监听

```python
class ESLListener:
    def __init__(self):
        self.esl_socket = None
        
    def connect_esl(self):
        """连接到 FreeSWITCH ESL"""
        import ESL
        self.esl_socket = ESL.ESLconnection("127.0.0.1", "8021", "ClueCon")
        
        # 订阅事件
        self.esl_socket.send("events json ALL")
        
    def handle_channel_events(self):
        """处理通道事件"""
        while True:
            e = self.esl_socket.recvEvent()
            if e:
                event_name = e.getName()
                
                if event_name == "CHANNEL_ANSWER":
                    # 通道应答事件
                    self.on_channel_answer(e)
                    
                elif event_name == "CHANNEL_HANGUP":
                    # 通道挂断事件
                    self.on_channel_hangup(e)
                    
                elif event_name == "DTMF":
                    # DTMF 按键事件
                    self.on_dtmf(e)
    
    def on_channel_answer(self, event):
        """处理通道应答"""
        call_uuid = event.getHeader("Unique-ID")
        print(f"通话 {call_uuid} 已接通")
        
    def on_channel_hangup(self, event):
        """处理通道挂断"""
        call_uuid = event.getHeader("Unique-ID")
        reason = event.getHeader("Hangup-Cause")
        print(f"通话 {call_uuid} 已挂断，原因: {reason}")
        
    def on_dtmf(self, event):
        """处理 DTMF 按键"""
        digit = event.getHeader("DTMF-Digit")
        call_uuid = event.getHeader("Unique-ID")
        print(f"通话 {call_uuid} 按键: {digit}")
```

## 5. 完整的 AI 外呼流程

### 5.1 发起 AI 外呼

```xml
<extension name="ai_outbound_call">
  <condition field="${direction}" expression="^outbound$" break="never"/>
  <condition field="${ai_agent}" expression="^true$" break="on-true">
    <action application="answer"/>
    <action application="set" data="media_bug_answer_req=true"/>
    <action application="set" data="RECORD_STEREO=false"/>
    <action application="set" data="record_file=$${recording_base_dir}/${task_id}/${uuid}.wav"/>
    <action application="record_session" data="${record_file}"/>
    <action application="audio_stream" data="ws://backend:$${audio_stream_port} 8000 read 20"/>
    <action application="socket" data="$${backend_socket_host}:$${backend_socket_port} async full"/>
  </condition>
</extension>
```

### 5.2 后端 AI 处理流程

1. **接收音频流**: 通过 WebSocket 接收来自 FreeSWITCH 的实时音频
2. **ASR 识别**: 将音频转换为文本
3. **LLM 处理**: 使用 AI 模型理解意图并生成回复
4. **TTS 合成**: 将 AI 回复转换为音频
5. **播放给被叫**: 通过 ESL 控制 FreeSWITCH 播放音频给被叫
6. **录音保存**: 保存通话录音用于后续分析

## 6. 安全考虑

### 6.1 认证与授权
- ESL 连接使用强密码
- WebSocket 连接可增加认证头
- SIP 注册启用鉴权

### 6.2 网络安全
- 使用防火墙限制访问端口
- 考虑使用 TLS 加密通信
- 配置 ACL 控制访问权限

## 7. 性能优化

### 7.1 音频质量
- 选择适当的编解码器 (PCMU/PCMA)
- 配置合适的 RTP 端口范围
- 优化网络 QoS 设置

### 7.2 并发处理
- 配置合适的并发呼叫限制
- 优化后端处理能力
- 实现负载均衡机制

## 8. 监控与调试

### 8.1 日志配置
- 配置详细的通话日志
- 记录 ASR/TTS 处理结果
- 监控系统性能指标

### 8.2 实时监控
- 使用 fs_cli 实时查看通话状态
- 监控 WebSocket 连接状态
- 跟踪 ESL 事件流

这个方案提供了完整的语音流转路径，确保语音可以从 Zoiper 软电话经过 FreeSWITCH 到达后端 AI 服务，同时支持 AI 生成的语音返回给通话双方。