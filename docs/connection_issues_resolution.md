# FreeSWITCH 与后端服务连接问题解决方案

## 问题描述
- 后端服务无法连接到 FreeSWITCH 的 ESL 端口 (8021)
- Zoiper 注册返回 408 错误
- FreeSWITCH 只在 127.0.0.1:8021 监听，而不是 0.0.0.0:8021
- 导致容器间无法建立连接

## 根本原因
FreeSWITCH 容器中的 event_socket 配置虽然设置为监听 0.0.0.0，但实际运行的进程只监听在 127.0.0.1 上，这在 Docker 容器网络中会导致其他容器无法连接。

## 解决方案

### 方案1：修改 Docker 启动参数
在 docker-compose.yml 中为 FreeSWITCH 服务添加网络模式：

```yaml
freeswitch:
  # ... 其他配置
  networks:
    - backend
  extra_hosts:
    - "host.docker.internal:host-gateway"  # 如果需要访问宿主机服务
  # 添加以下参数
  sysctls:
    - net.core.somaxconn=1024
  # 确保端口正确暴露
  ports:
    - "8021:8021"  # ESL 端口
    - "5060:5060"  # SIP 端口
    # ... 其他端口
```

### 方案2：使用自定义启动脚本
创建一个启动脚本来确保配置正确加载：

```bash
#!/bin/bash
# 等待 FreeSWITCH 启动后，通过 API 或其他方式重新加载配置

# 1. 启动 FreeSWITCH
/usr/local/freeswitch/bin/freeswitch -nonat -nosql &

# 2. 等待服务启动
sleep 10

# 3. 检查并确保 ESL 在正确的接口上监听
# （如果支持的话）
# fs_cli -p ClueCon -H 127.0.0.1 -P 8021 -x "reload mod_event_socket"

# 4. 持续监控
wait
```

### 方案3：使用启动后配置重载（推荐）
由于当前配置文件挂载方式已经正确，问题可能是启动顺序或模块加载时机。我们可以：

1. 确保 FreeSWITCH 完全启动后再启动后端服务
2. 在后端服务中实现更健壮的 ESL 连接重试机制

### 实施步骤

1. **更新 docker-compose.yml** 确保正确的依赖关系：
```yaml
backend:
  # ... 其他配置
  depends_on:
    - postgres
    - redis
    - freeswitch  # 添加对 FreeSWITCH 的依赖
  environment:
    # 确保后端连接到正确的服务名
    FS_HOST: freeswitch
    # ... 其他环境变量
```

2. **增强后端 ESL 服务的重试机制**：
   当前后端服务应该在启动时等待 FreeSWITCH 准备就绪后再尝试连接。

3. **验证网络连通性**：
   - 确保两个容器在同一个 Docker 网络中
   - 验证服务名解析是否正常

## 音频流传输机制

FreeSWITCH 通过以下方式传输音频到后端：
1. **音频流路径**：被叫 → FreeSWITCH → mod_audio_stream → WebSocket (ws://backend:8765) → ASR 服务
2. **控制路径**：后端 → ESL (8021) → FreeSWITCH
3. **外呼控制**：后端 → ESL (8021) → 发起外呼 → FreeSWITCH → 被叫
4. **外呼应答后**：FreeSWITCH → ESL (9999) → 后端 CallAgent

## 临时诊断命令

```bash
# 检查 FreeSWITCH 监听状态
docker exec outbound_freeswitch netstat -tulnp | grep 8021

# 检查容器间网络连通性
docker exec outbound_backend ping freeswitch
docker exec outbound_backend nc -zv freeswitch 8021

# 检查后端日志
docker logs outbound_backend | grep -i "esl\|connection\|refused"

# 检查 FreeSWITCH 日志
docker logs outbound_freeswitch | grep -i "event\|socket\|8021"
```

## 推荐解决步骤

1. 重新启动整个服务栈，确保启动顺序正确
2. 检查后端服务的 ESL 连接重试逻辑
3. 验证 FreeSWITCH 配置文件是否正确加载
4. 检查 Zoiper 注册配置
5. 验证音频流传输路径