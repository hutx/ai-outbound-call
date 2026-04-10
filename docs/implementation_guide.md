# FreeSWITCH 语音路由实施指南

## 项目背景
本指南介绍如何在 AI 外呼系统中实施 FreeSWITCH 语音路由，实现 Zoiper 软电话与后端 AI 服务之间的语音流转。

## 1. 系统架构概览

### 1.1 组件说明
- **FreeSWITCH**: SIP 服务器和媒体网关，处理语音路由
- **Zoiper**: 软电话客户端，供人工坐席使用
- **后端 AI 服务**: 处理 ASR、LLM、TTS 的服务

### 1.2 语音流路径
```
Zoiper ←→ FreeSWITCH ←→ 运营商/被叫 ←→ FreeSWITCH ←→ 后端 AI 服务
```

## 2. 配置实施步骤

### 2.1 FreeSWITCH 配置文件部署

将以下配置文件复制到相应目录：

```bash
# 复制示例配置
cp example_config.xml /etc/freeswitch/dialplan/ai_routing.xml
cp example_config.xml /etc/freeswitch/sip_profiles/ai_gateway.xml

# 或者将内容合并到现有配置文件中
```

### 2.2 Zoiper 软电话配置

#### 2.2.1 基本账户设置
- SIP 服务器: `你的_FreeSWITCH_IP`
- 端口: `5060`
- 传输: `UDP`
- 用户名: `1001` (或其他在 directory/default.xml 中定义的分机号)
- 密码: `1001` (或对应密码)
- 域: `你的_FreeSWITCH_IP`

#### 2.2.2 高级设置
- 编解码器顺序: PCMU, PCMA, G729
- 采样率: 8000 Hz
- 启用 STUN (如在 NAT 环境下)

### 2.3 后端服务配置

#### 2.3.1 WebSocket 音频接收服务
```javascript
// 后端 WebSocket 服务示例 (Node.js)
const WebSocket = require('ws');
const wss = new WebSocket.Server({ port: 8765 });

wss.on('connection', function connection(ws) {
  ws.on('message', function incoming(data) {
    // 处理来自 FreeSWITCH 的音频流
    console.log('Received audio data:', data.length, 'bytes');
    
    // 转发到 ASR 服务
    processAudioForASR(data);
  });
});
```

#### 2.3.2 ESL 连接服务
```python
# Python ESL 示例
import ESL

def connect_to_freeswitch():
    con = ESL.ESLconnection("127.0.0.1", "8021", "ClueCon")
    
    if con.connected():
        print("Connected to FreeSWITCH ESL")
        
        # 发送命令
        response = con.sendRecv("status")
        print(response.getBody())
        
        return con
    else:
        print("Failed to connect to ESL")
        return None
```

## 3. 语音路由配置详解

### 3.1 内部通话路由
```xml
<extension name="internal_call">
  <condition field="destination_number" expression="^(\d{4})$">
    <action application="export" data="sip_auto_answer=true"/>
    <action application="answer"/>
    <action application="bridge" data="user/$1@${domain_name}"/>
  </condition>
</extension>
```

### 3.2 AI 外呼路由
```xml
<extension name="ai_outbound_processing">
  <condition field="${direction}" expression="^outbound$" break="never"/>
  <condition field="${ai_agent}" expression="^true$" break="on-true">
    <action application="answer"/>
    <action application="set" data="media_bug_answer_req=true"/>
    <action application="audio_stream" data="ws://backend:8765 8000 read 20"/>
    <action application="socket" data="backend:9999 async full"/>
  </condition>
</extension>
```

### 3.3 转人工坐席路由
```xml
<extension name="transfer_to_human_agent">
  <condition field="destination_number" expression="^human_agent_(\d+)$" break="on-true">
    <action application="bridge" data="user/$1@${domain_name}"/>
  </condition>
</extension>
```

## 4. 测试步骤

### 4.1 基础连通性测试
```bash
# 1. 检查 FreeSWITCH 状态
fs_cli
show version
sofia status

# 2. 测试 Zoiper 注册
# 在 Zoiper 中尝试注册到 FreeSWITCH

# 3. 测试内部分机通话
# 从分机 1001 拨打 1002
```

### 4.2 AI 外呼测试
```bash
# 通过 ESL 发起 AI 外呼
fs_cli
originate {ai_agent=true,task_id=test123}sofia/gateway/carrier_trunk/13800138000 &socket(backend:9999 async full)
```

### 4.3 语音流测试
```bash
# 1. 检查 WebSocket 连接
# 查看后端日志是否收到音频流

# 2. 检查录音文件
ls -la /recordings/test123/

# 3. 验证 ESL 通信
# 查看后端 ESL 日志
```

## 5. 故障排除

### 5.1 常见问题及解决方案

#### 问题1: Zoiper 无法注册
- **症状**: 注册失败或频繁掉线
- **解决**:
  - 检查用户名密码是否正确
  - 检查网络连通性
  - 查看 FreeSWITCH 日志: `tail -f /var/log/freeswitch/freeswitch.log`

#### 问题2: 语音单通或无声
- **症状**: 只能听到一方声音
- **解决**:
  - 检查 NAT 配置
  - 验证 RTP 端口是否开放
  - 确认编解码器兼容性

#### 问题3: AI 外呼无法连接后端
- **症状**: 外呼接通但 AI 无响应
- **解决**:
  - 检查 WebSocket 连接
  - 验证 ESL 端口可达性
  - 查看后端服务日志

### 5.2 调试命令
```bash
# 启用 SIP 跟踪
fs_cli
console loglevel 7
sofia global tracelevel 9
sofia global siptrace on

# 查看当前通话
show calls

# 查看网关状态
sofia status gateway carrier_trunk

# 测试音频流
fs_cli
uuid_record <call_uuid> /tmp/test_recording.wav
```

## 6. 性能优化建议

### 6.1 音频质量优化
- 使用 G.711 编解码器 (PCMU/PCMA) 以获得最佳 ASR 效果
- 设置合适的 RTP 采样率 (8000 Hz 用于电话质量)
- 配置 QoS 保证语音优先级

### 6.2 并发处理优化
- 调整并发呼叫限制
- 优化后端处理能力
- 配置适当的缓冲区大小

### 6.3 安全配置
- 使用强 ESL 密码
- 配置防火墙限制访问
- 启用必要的认证机制

## 7. 监控与维护

### 7.1 关键监控指标
- 呼叫成功率
- 语音质量 (丢包率、抖动)
- 后端响应时间
- 系统资源使用率

### 7.2 日志分析
- 定期检查 FreeSWITCH 日志
- 分析通话记录 (CDR)
- 监控 ASR/TTS 服务日志

通过按照本指南实施配置，你可以建立一个完整的语音路由系统，实现 Zoiper 软电话与后端 AI 服务之间的高效语音流转。