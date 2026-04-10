# AI 外呼系统部署指南

## 概述

本文档介绍如何部署包含 mod_audio_stream 模块的增强版 FreeSWITCH 镜像，以支持完整的 AI 外呼功能。

## 问题分析

通过日志分析，我们发现以下关键问题：

1. **mod_audio_stream 模块缺失**：
   ```
   [CRIT] Error Loading module /usr/lib/freeswitch/mod/mod_audio_stream.so
   **/usr/lib/freeswitch/mod/mod_audio_stream.so: cannot open shared object file: No such file or directory**
   ```

2. **ESL 监听问题**：ESL 服务只监听在 127.0.0.1 而非 0.0.0.0

3. **网络配置问题**：IP 地址配置不匹配

## 解决方案

### 1. 构建增强版镜像

使用提供的构建脚本构建包含 mod_audio_stream 模块的 FreeSWITCH 镜像：

```bash
# 进入 docker 目录
cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new/docker

# 运行构建脚本
./build_enhanced_freeswitch.sh
```

构建脚本将：
- ✅ 从 safarov/freeswitch:latest 基础镜像构建
- ✅ 安装构建依赖
- ✅ 下载并编译 mod_audio_stream 模块
- ✅ 配置 ESL 监听在 0.0.0.0:8021
- ✅ 配置 audio_stream 模块

### 2. 更新 docker-compose.yml

构建完成后，需要更新 docker-compose.yml 文件中的 FreeSWITCH 服务配置：

```yaml
# 在 docker/docker-compose.yml 中修改 freeswitch 服务
freeswitch:
  # 使用构建的增强版镜像
  image: freeswitch-ai-enhanced:latest
  container_name: outbound_freeswitch
  restart: unless-stopped
  
  # 端口映射保持不变
  ports:
    # ESL 端口 - 用于与后端服务通信
    - "8021:8021"
    # ESL 传输端口
    - "8022:8022"
    # SIP 端口
    - "5060:5020"  # 注意：这里可能需要调整
    - "5060:5060/udp"
    - "5080:5080"
    - "5080:5080/udp"
    # RTP 媒体流端口范围（重要：必须映射以支持语音通话）
    - "16384-16484:16384-16484/udp"
    - "16485-16585:16485-16585/udp"
    - "16586-16686:16586-16686/udp"
    - "16687-16787:16687-16787/udp"
    - "16788-16888:16788-16888/udp"
    - "16889-17384:16889-17384/udp"
  
  # 配置挂载卷
  volumes:
    # 挂载整个 conf 目录
    - ../freeswitch/conf:/usr/local/freeswitch/conf
    - ../freeswitch/scripts:/usr/share/freeswitch/scripts
    # 录音文件
    - ./docker/recordings:/recordings
    - /Users/hutx/work/freeswitch/sounds:/usr/share/freeswitch/sounds
    # 日志 
    - ./logs/freeswitch:/var/log/freeswitch
    - /usr/share/alsa:/usr/share/alsa:ro
    # 时区文件
    - /etc/localtime:/etc/localtime:ro
    - /etc/timezone:/etc/timezone:ro
  
  # 安全和性能设置
  cap_add:
    - SYS_NICE    # 设置进程优先级（实时音频需要）
    - NET_ADMIN   # NAT 穿透
    - IPC_LOCK    # 锁定共享内存页（防止被 swap，降低延迟）
  
  ulimits:
    nofile:
      soft: 65536
      hard: 65536
    # 实时优先级上限
    rtprio:
      soft: 99
      hard: 99
  
  # 环境变量
  environment:
    JACK_NO_AUDIO_RESERVATION: "1"
    TZ: Asia/Shanghai
    FS_EXTERNAL_IP: 219.143.45.154
    SOUND_RATES: 8000:16000
    SOUND_TYPES: music:en-us-callie
  
  # 健康检查
  healthcheck:
    test: ["CMD", "fs_cli", "-x", "status"]
    interval: 30s
    timeout: 10s
    retries: 5
    start_period: 15s
  
  networks:
    - backend
```

### 3. 配置文件调整

确保以下配置文件正确配置：

#### vars.xml
```xml
<!-- SIP 域名称设置为主机 IP -->
<X-PRE-PROCESS cmd="set" data="sip_domain=192.168.5.15" />
<!-- 内部扩展 IP 设置为主机 IP -->
<X-PRE-PROCESS cmd="set" data="internal_ext_sip_ip=192.168.5.15" />
<X-PRE-PROCESS cmd="set" data="internal_ext_rtp_ip=192.168.5.15" />
```

#### audio_stream.conf.xml
```xml
<configuration name="audio_stream.conf" description="实时音频流推送">
  <settings>
    <param name="default-url"     value="ws://backend:8765"/>
    <param name="default-rate"    value="8000"/>
    <param name="default-ms"      value="20"/>
    <param name="default-channel" value="read"/>
  </settings>
</configuration>
```

#### event_socket.conf.xml
```xml
<configuration name="event_socket.conf" description="Socket Client">
  <settings>
    <param name="listen-ip" value="0.0.0.0"/>  <!-- 修复：监听所有接口 -->
    <param name="listen-port" value="8021"/>
    <param name="password" value="ClueCon"/>
  </settings>
</configuration>
```

## 部署步骤

### 1. 停止当前服务
```bash
cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new
docker-compose -f docker/docker-compose.yml down
```

### 2. 构建增强版镜像
```bash
cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new/docker
./build_enhanced_freeswitch.sh
```

### 3. 启动服务
```bash
cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new
docker-compose -f docker/docker-compose.yml up -d
```

### 4. 验证部署

#### 检查模块加载
```bash
docker logs outbound_freeswitch | grep -i "audio_stream"
```

#### 检查端口监听
```bash
docker exec outbound_freeswitch netstat -tulnp | grep 8021
# 应该显示 0.0.0.0:8021 而不是 127.0.0.1:8021
```

#### 测试 ESL 连接
```bash
docker exec outbound_backend python3 -c "
import socket
s = socket.socket()
result = s.connect_ex(('freeswitch', 8021))
print('ESL 连接结果:', result)  # 0 表示成功
s.close()
"
```

#### 查看后端日志
```bash
docker logs outbound_backend | grep -i "esl\|connection\|success"
```

## Zoiper 配置

使用以下参数配置 Zoiper：

- **服务器/域**: 192.168.5.15
- **用户名**: 1001 或 1002（根据配置）
- **密码**: 1001 或 1002（根据配置）
- **端口**: 5060
- **协议**: UDP

## AI 外呼功能测试

1. **确保后端服务正常运行**：
   ```bash
   docker logs outbound_backend | grep -i "websocket\|ready\|listening"
   ```

2. **发起测试外呼**：
   - 通过后端 API 发起外呼请求
   - 检查后端日志中的 ASR/TTS/LLM 处理记录

3. **验证音频流传输**：
   - 检查后端 WebSocket 服务器是否收到音频流
   - 确认 ASR 服务能正常识别语音

## 故障排除

### 1. 模块未加载
如果 `mod_audio_stream` 模块仍未加载，请检查：
- Dockerfile 中的构建步骤是否成功
- 模块文件是否存在：`/usr/local/freeswitch/mod/mod_audio_stream.so`
- modules.conf.xml 是否包含模块加载指令

### 2. ESL 连接失败
如果 ESL 连接失败：
- 确认 event_socket.conf.xml 配置正确
- 检查端口监听状态
- 验证容器间网络连通性

### 3. Zoiper 注册失败
- 检查 IP 地址配置
- 验证用户账户配置
- 确认防火墙设置

## 验证清单

- [ ] 增强版镜像构建成功
- [ ] mod_audio_stream 模块正确加载
- [ ] ESL 监听在 0.0.0.0:8021
- [ ] Zoiper 能成功注册
- [ ] 内部分机通话正常
- [ ] 后端能连接到 ESL
- [ ] 音流能正确传输到后端
- [ ] AI 外呼功能正常

## 后续步骤

1. 进行完整的功能测试
2. 调优性能参数
3. 配置生产环境参数
4. 设置监控和日志收集