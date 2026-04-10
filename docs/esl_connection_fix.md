# ESL 连接问题修复方案

## 问题根源
FreeSWITCH 容器中的 event_socket 模块只监听在 `127.0.0.1:8021`，而不是 `0.0.0.0:8021`，导致其他容器无法连接。

## 修复方法

### 方法1：使用 FreeSWITCH CLI 重新加载模块

在 FreeSWITCH 启动后，使用 CLI 命令重新加载 event_socket 模块：

```bash
# 连接到 FreeSWITCH CLI 并重新加载模块
fs_cli -p ClueCon -H 127.0.0.1 -P 8021 -x "reload mod_event_socket"
```

### 方法2：修改 docker-compose.yml 使用启动命令覆盖

修改 docker-compose.yml 中的 freeswitch 服务：

```yaml
freeswitch:
  # ... 其他配置
  command: >
    sh -c "
    # 等待配置文件准备就绪
    sleep 5 &&
    # 启动 FreeSWITCH
    /usr/local/freeswitch/bin/freeswitch -nonat -nosql -c
    "
```

### 方法3：创建自定义镜像（推荐）

创建一个自定义的 Dockerfile 来确保配置正确：

```dockerfile
FROM servicebots/freeswitch:latest

# 复制修复脚本
COPY fix_esl_config.sh /tmp/fix_esl_config.sh
RUN chmod +x /tmp/fix_esl_config.sh

# 运行修复脚本
RUN /tmp/fix_esl_config.sh

# 使用修改过的启动命令
CMD ["/usr/local/freeswitch/bin/freeswitch", "-nonat", "-nosql", "-c"]
```

### 方法4：使用 FreeSWITCH 的启动脚本机制

创建一个启动脚本，在 FreeSWITCH 启动后自动执行配置修复：

1. 创建启动脚本 `esl_fix_init.lua`:
```lua
-- ESL 配置修复脚本
freeswitch.console_log("info", "Applying ESL configuration fix...\n")

-- 这里可以添加需要的配置修复命令
-- 例如重新加载模块等
```

2. 在启动时加载此脚本

### 方法5：使用健康检查和初始化容器

在 docker-compose.yml 中添加初始化容器：

```yaml
services:
  # ... 其他服务
  
  freeswitch-init:
    image: busybox:latest
    command: >
      sh -c "
      # 等待 FreeSWITCH 启动
      sleep 10 &&
      # 使用 docker exec 命令修复配置
      # 或者通过其他方式重新加载模块
      "
    depends_on:
      - freeswitch
  
  backend:
    # ... 后端配置
    depends_on:
      - freeswitch-init  # 等待初始化完成
```

## 临时修复命令

如果您需要立即修复，请执行以下命令序列：

```bash
# 1. 停止当前服务
cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new
docker-compose -f docker/docker-compose.yml down

# 2. 手动启动 FreeSWITCH 并尝试重新加载模块
# 由于在容器中可能没有 fs_cli，我们使用以下方法

# 3. 重新启动服务
docker-compose -f docker/docker-compose.yml up -d freeswitch
sleep 30

# 检查端口监听状态
docker exec outbound_freeswitch netstat -tulnp | grep 8021
```

## 验证修复

修复后，执行以下验证：

1. 检查端口监听：
```bash
docker exec outbound_freeswitch netstat -tulnp | grep 8021
# 应该显示 0.0.0.0:8021 而不是 127.0.0.1:8021
```

2. 从后端容器测试连接：
```bash
docker exec outbound_backend python3 -c "
import socket
s = socket.socket()
result = s.connect_ex(('freeswitch', 8021))
print('Connection result:', result)  # 0 表示成功
s.close()
"
```

## Zoiper 注册问题

一旦 ESL 连接问题解决，Zoiper 的 408 错误可能也会得到解决，因为这通常是由于 FreeSWITCH 服务不稳定导致的。

## 音频流传输

FreeSWITCH 通过 mod_audio_stream 将音频流推送到后端：
- 配置：`audio_stream` 应用调用 `ws://backend:8765`
- 后端 WebSocket 服务器：监听在 8765 端口
- 这部分应该已经正常工作，因为我们已经在后端日志中看到 WebSocket 服务器启动

## 推荐解决方案

鉴于当前情况，我推荐使用**方法3：创建自定义镜像**，因为它能确保配置的持久性和一致性。

## 后续步骤

1. 实施上述修复方案之一
2. 重新测试 ESL 连接
3. 验证 Zoiper 注册
4. 测试 AI 外呼功能
5. 确认音频流传输正常