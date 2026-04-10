# AI 外呼系统完整实施指南

## 问题诊断与解决方案

### 当前问题分析
1. **内部通话无语音**：internal.xml 缺少内部分机直拨路由
2. **音频流未到达后端**：audio_stream 配置可能有问题
3. **AI 外呼流程中断**：拨号计划配置不完整

### 解决方案概览
- 重构 internal 拨号计划，添加完整的内部分机路由
- 优化 audio_stream 配置，确保音频流正确传递
- 完善 AI 外呼流程，确保各环节连通

## 完整配置方案

### 1. 内部分机拨号计划优化 (dialplan/internal.xml)

已更新配置包含：
- 4位/3位分机直拨
- 内线拨打外线 (9+手机号)
- AI 外呼处理
- 转人工坐席
- 会议桥接
- 语音信箱等功能

### 2. 音频流配置优化

#### audio_stream.conf.xml 优化
```xml
<configuration name="audio_stream.conf" description="实时音频流推送">
  <settings>
    <param name="default-url"     value="ws://backend:8765"/>
    <param name="default-rate"    value="8000"/>
    <param name="default-ms"      value="20"/>
    <param name="default-channel" value="read"/>
    <param name="connect-timeout" value="3000"/>
    <param name="reconnect-interval-ms" value="1000"/>
    <!-- 添加错误处理 -->
    <param name="error-on-timeout" value="false"/>
    <param name="error-on-failure" value="false"/>
  </settings>
</configuration>
```

### 3. AI 外呼流程优化

#### 拨号计划优化要点
1. **媒体参数设置**：确保 `media_bug_answer_req=true`
2. **音频流推送**：`audio_stream` 应用正确配置
3. **ESL 连接**：socket 应用连接后端 CallAgent
4. **录音配置**：合规录音同时进行

## 配置检查清单

### 1. 确认模块加载
检查 `autoload_configs/modules.conf.xml` 包含：
```xml
<load module="mod_audio_stream"/>
<load module="mod_dptools"/>
<load module="mod_commands"/>
<load module="mod_conference"/>
```

### 2. 确认变量配置
检查 `vars.xml` 包含：
```xml
<X-PRE-PROCESS cmd="set" data="audio_stream_port=8765" />
<X-PRE-PROCESS cmd="set" data="backend_socket_host=backend" />
<X-PRE-PROCESS cmd="set" data="backend_socket_port=9999" />
<X-PRE-PROCESS cmd="set" data="recording_base_dir=/recordings" />
```

### 3. 确认 Sofia Profile 配置
internal profile 应设置 context="internal"
outbound profile 应设置 context="outbound_calls"

## 启动和验证步骤

### 1. 启动顺序
```bash
# 1. 启动后端服务
cd /path/to/project
uv run python -m backend.api.main

# 2. 启动 FreeSWITCH
sudo freeswitch -nonat -nosql

# 3. 验证连接
fs_cli
```

### 2. 功能验证

#### 内部通话验证
1. 用 Zoiper 注册分机 1001
2. 用 Zoiper 注册分机 1002
3. 从 1001 拨打 1002，应能正常通话

#### AI 外呼验证
1. 通过 API 发起 AI 外呼任务
2. 检查后端日志是否收到 WebSocket 音频流
3. 验证 ASR/TTS/LLM 处理流程

#### 音频流验证
1. 检查后端 WebSocket 服务器是否收到音频流
2. 验证音频数据格式（8000Hz, 16bit, mono PCM）
3. 确认 ASR 服务能正常处理音频

## 常见问题排查

### 1. Zoiper 无法通话
- 检查防火墙是否开放 5060-5080 端口
- 确认 NAT 配置是否正确
- 检查编解码器设置

### 2. 音频流未到达后端
- 检查 mod_audio_stream 是否正确安装
- 验证 WebSocket 服务器是否运行
- 确认 network connectivity (backend:8765)

### 3. AI 外呼无响应
- 检查 ESL 连接是否正常
- 验证后端 CallAgent 是否运行
- 确认 channel variables 传递正确

## 监控和日志

### 关键日志文件
- `/var/log/freeswitch/freeswitch.log` - FreeSWITCH 主日志
- `/var/log/freeswitch/ai_outbound.log` - AI 外呼日志
- 后端应用日志 - 用于调试音频流和 AI 处理

### 监控命令
```bash
# FreeSWITCH CLI 监控
fs_cli
show calls          # 显示当前通话
sofia status        # 显示 sofia 状态
show registrations  # 显示注册状态

# 后端监控
curl http://localhost:8000/monitor/stats  # 获取通话统计
```

## 性能优化建议

### 1. 音频质量优化
- 使用 G.711 编解码器 (PCMU/PCMA) 保证 ASR 效果
- 设置合适采样率 (8000Hz)
- 配置 QoS 保证语音优先级

### 2. 并发处理优化
- 调整并发呼叫限制
- 优化后端处理能力
- 配置适当的缓冲区大小

### 3. 网络优化
- 使用专用网络段
- 配置防火墙 QoS
- 考虑使用 STUN/TURN 服务

通过以上配置和验证步骤，您的 AI 外呼系统应该能够正常工作，包括内部分机通话和 AI 外呼功能。