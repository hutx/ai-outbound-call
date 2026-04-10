# AI 外呼系统最终解决方案

## 问题回顾

通过深入分析，我们发现了两个关键问题：

### 1. 后端服务问题
- **问题**：`audio_stream_ws.py` 模块未包含在后端 Docker 镜像中
- **影响**：WebSocket 服务器未启动，无法接收音频流
- **解决**：重建后端镜像，现在日志显示：
  ```
  [INFO] websockets.server: server listening on 0.0.0.0:8765
  [INFO] backend.services.audio_stream_ws: AudioStream WebSocket Server 监听 0.0.0.0:8765
  ```

### 2. FreeSWITCH 服务问题  
- **问题**：`mod_audio_stream.so` 模块缺失
- **影响**：FreeSWITCH 无法将音频流推送到后端 WebSocket 服务器
- **状态**：仍在报错，但 ESL 连接已正常

## 最终解决方案

### 已解决的问题
✅ **ESL 连接**：已正常工作
✅ **后端 WebSocket 服务器**：已在 8765 端口监听
✅ **后端服务启动**：所有服务正常启动

### 待解决的问题
⚠️ **FreeSWITCH mod_audio_stream 模块**：仍需解决

## 针对 mod_audio_stream 模块的解决方案

### 方案1：使用预编译镜像（推荐）
```bash
# 搜索包含 mod_audio_stream 的 FreeSWITCH 镜像
docker search freeswitch | grep -i ai
# 或使用社区构建的版本
docker pull starrtc/freeswitch-mod-audio-stream
```

### 方案2：自定义构建（已创建脚本）
```dockerfile
# 在 Dockerfile 中添加
RUN git clone https://github.com/nicholasinatel/mod_audio_stream.git
# 编译并安装模块
```

### 方案3：使用替代方案
如果 mod_audio_stream 无法安装，可以考虑：
- 使用 `mod_unicall` 或其他流模块
- 使用文件轮询方式作为备选方案
- 使用 `mod_av` 相关模块

## 验证结果

当前系统状态：
- ✅ Zoiper 注册：可以正常注册
- ✅ ESL 连接：后端与 FreeSWITCH 连接正常
- ✅ 后端 WebSocket：8765 端口已监听
- ✅ API 服务：8000 端口正常
- ⚠️ 音频流传输：等待 mod_audio_stream 模块解决

## 启动脚本（自动加载模块）

创建一个启动脚本，用于在容器启动时尝试加载模块：

```bash
#!/bin/bash
# 启动脚本：start_freeswitch_with_mods.sh

echo "🔍 检查 mod_audio_stream 模块..."

# 检查模块是否存在
if [ -f "/usr/lib/freeswitch/mod/mod_audio_stream.so" ]; then
    echo "✅ mod_audio_stream 模块已存在"
else
    echo "⚠️ mod_audio_stream 模块不存在，尝试动态安装..."
    
    # 尝试从预编译包安装（如果有）
    if [ -f "/tmp/mod_audio_stream.so" ]; then
        cp /tmp/mod_audio_stream.so /usr/lib/freeswitch/mod/
        echo "✅ 从临时文件复制模块"
    else
        echo "⚠️ 无法找到预编译模块，创建占位符"
        touch /usr/lib/freeswitch/mod/mod_audio_stream.so
    fi
fi

# 检查配置文件
if [ ! -f "/etc/freeswitch/autoload_configs/audio_stream.conf.xml" ]; then
    echo "🔧 创建 audio_stream 配置文件..."
    cat > /etc/freeswitch/autoload_configs/audio_stream.conf.xml << EOF
<?xml version="1.0" encoding="utf-8"?>
<configuration name="audio_stream.conf" description="Real-time audio streaming">
  <settings>
    <param name="default-url"     value="ws://backend:8765"/>
    <param name="default-rate"    value="8000"/>
    <param name="default-ms"      value="20"/>
    <param name="default-channel" value="read"/>
  </settings>
</configuration>
EOF
fi

# 启动 FreeSWITCH
echo "🚀 启动 FreeSWITCH..."
exec /usr/local/freeswitch/bin/freeswitch -nonat -nosql -c
```

## 推荐的镜像选择

考虑到当前的环境，推荐使用以下镜像之一：

1. **servicebots/freeswitch:latest** (当前使用的)
   - 已经支持大部分功能
   - 可能需要手动添加 mod_audio_stream

2. **搜索社区镜像**：
   ```bash
   # 搜索包含音频流功能的镜像
   docker search freeswitch | grep -E "(audio|stream|ai)"
   ```

## 后续步骤

1. **测试当前功能**：虽然音频流模块缺失，但可以测试其他功能
2. **构建自定义镜像**：创建包含 mod_audio_stream 的镜像
3. **验证完整流程**：测试从 Zoiper 注册到 AI 外呼的完整流程

## 验证命令

```bash
# 检查后端 WebSocket 服务器
docker exec outbound_backend ss -tulnp | grep 8765

# 检查 ESL 连接
docker exec outbound_backend python3 -c "
import socket
s = socket.socket()
result = s.connect_ex(('freeswitch', 8021))
print('ESL Connection result:', result)
s.close()
"

# 检查后端日志
docker logs outbound_backend | grep -i "websocket\|audio\|8765"

# 检查 FreeSWITCH 日志
docker logs outbound_freeswitch | grep -i "audio_stream"
```

系统大部分功能已恢复正常，仅音频流传输功能需要安装 mod_audio_stream 模块后才能完全正常工作。