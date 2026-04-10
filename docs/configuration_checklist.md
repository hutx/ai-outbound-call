# 配置检查清单

## 1. FreeSWITCH 服务状态检查

### 检查 FreeSWITCH 是否运行
```bash
# 检查进程
ps aux | grep freeswitch

# 检查端口监听
netstat -tlnp | grep -E "(5060|5080|8021)"

# 连接 FreeSWITCH CLI
fs_cli
```

### 检查 Sofia 状态
```bash
# 在 fs_cli 中执行
sofia status
sofia status profile internal
sofia status profile outbound
show registrations
```

## 2. 模块加载检查

### 确认必要模块已加载
```bash
# 在 fs_cli 中执行
show modules
# 检查以下模块是否存在：
# - mod_audio_stream
# - mod_dptools  
# - mod_commands
# - mod_conference
```

## 3. 拨号计划检查

### 验证拨号计划加载
```bash
# 在 fs_cli 中执行
reloadxml
show dialplan internal
show dialplan outbound_calls
show dialplan ai_outbound
```

## 4. 网关状态检查

### 检查运营商网关
```bash
# 在 fs_cli 中执行
sofia status gateway carrier_trunk
```

## 5. 后端服务检查

### 检查后端服务状态
```bash
# 检查后端 API 服务
curl -v http://localhost:8000/health

# 检查 ESL 服务端口
nc -zv localhost 9999
nc -zv localhost 8021

# 检查 WebSocket 服务端口
nc -zv localhost 8765
```

## 6. Zoiper 软电话配置检查

### 分机注册验证
- 确认 Zoiper 能成功注册到 FreeSWITCH
- 检查分机号码（1001, 1002 等）
- 验证用户名密码是否正确

### 通话测试
- 从分机 1001 拨打 1002
- 验证双向音频是否正常
- 测试拨打 9+手机号（如 913800138000）

## 7. 音频流检查

### WebSocket 连接测试
```bash
# 检查 WebSocket 服务器是否运行
curl -v http://localhost:8765
```

### 音频流验证
- 发起一个通话
- 检查后端日志是否有音频流接收记录
- 验证音频格式是否为 8000Hz 16bit mono PCM

## 8. AI 外呼功能检查

### 发起测试外呼
```bash
# 通过 ESL 发起测试呼叫
fs_cli
originate {ai_agent=true,task_id=test,script_id=default}sofia/gateway/carrier_trunk/13800138000 &socket(127.0.0.1:9999 async full)
```

### 检查后端处理
- 确认 CallAgent 实例创建
- 验证音频流到达 ASR 服务
- 检查 TTS 音频播放

## 9. 日志检查

### 关键日志文件
```bash
# FreeSWITCH 日志
tail -f /var/log/freeswitch/freeswitch.log

# AI 外呼日志
tail -f /var/log/freeswitch/ai_outbound.log

# 后端应用日志
tail -f logs/backend.log  # 如果存在
```

## 10. 网络连通性检查

### 服务间连通性
```bash
# 检查 FreeSWITCH 到后端的连接
telnet backend 9999  # 如果后端服务在 backend 容器
telnet localhost 9999  # 如果后端服务在本地

# 检查 WebSocket 连接
telnet backend 8765
telnet localhost 8765
```

## 11. 常见问题排查

### 问题1: Zoiper 注册失败
- 检查分机配置 (directory/default.xml)
- 确认用户名密码
- 检查网络连通性

### 问题2: 内部通话无声音
- 检查拨号计划 (dialplan/internal.xml)
- 确认 bridge 应用配置
- 检查媒体路径

### 问题3: 音频流未到达后端
- 检查 mod_audio_stream 配置
- 确认 audio_stream 应用在拨号计划中
- 验证 WebSocket 服务器状态

### 问题4: AI 外呼无响应
- 检查 ESL 连接
- 确认后端 CallAgent 服务
- 验证 channel variables

## 12. 验证完成清单

- [ ] FreeSWITCH 服务正常运行
- [ ] Sofia profiles 状态正常
- [ ] 必要模块已加载
- [ ] 拨号计划正确加载
- [ ] Zoiper 能成功注册
- [ ] 内部分机通话正常
- [ ] 音频流能到达后端
- [ ] AI 外呼功能正常
- [ ] 日志显示正常运行

完成以上所有检查后，您的 AI 外呼系统应该能够正常工作。