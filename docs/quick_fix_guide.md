# AI 外呼系统快速修复指南

## 问题概述

通过日志分析，发现以下关键问题：
1. **mod_audio_stream 模块缺失** - 音频流无法传输到后端
2. **ESL 监听配置错误** - 只监听在 127.0.0.1 而非 0.0.0.0
3. **配置文件路径问题** - IP 地址配置不匹配

## 快速修复方案

### 方案1：修复当前运行的容器

1. **复制修复脚本到容器**：
   ```bash
   docker cp /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new/docker/fix_mod_audio_stream.sh outbound_freeswitch:/tmp/
   ```

2. **在容器内执行修复**：
   ```bash
   docker exec -it outbound_freeswitch bash /tmp/fix_mod_audio_stream.sh
   ```

3. **重启 FreeSWITCH 服务**：
   ```bash
   docker restart outbound_freeswitch
   ```

### 方案2：直接修改容器配置（推荐）

1. **进入容器**：
   ```bash
   docker exec -it outbound_freeswitch bash
   ```

2. **修复 event_socket 配置**：
   ```bash
   # 编辑 event_socket 配置文件
   sed -i 's/127.0.0.1/0.0.0.0/g' /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml
   ```

3. **创建 audio_stream 配置**：
   ```bash
   cat > /usr/local/freeswitch/conf/autoload_configs/audio_stream.conf.xml << EOF
<?xml version="1.0" encoding="utf-8"?>
<configuration name="audio_stream.conf" description="Real-time audio streaming">
  <settings>
    <param name="default-url"     value="ws://backend:8765"/>
    <param name="default-rate"    value="8000"/>
    <param name="default-ms"      value="20"/>
    <param name="default-channel" value="read"/>
    <param name="connect-timeout" value="3000"/>
    <param name="reconnect-interval-ms" value="1000"/>
  </settings>
</configuration>
EOF
   ```

4. **确保模块加载**：
   ```bash
   # 检查 modules.conf.xml
   if ! grep -q "mod_audio_stream" /usr/local/freeswitch/conf/autoload_configs/modules.conf.xml; then
       sed -i '/<modules>/a <load module="mod_audio_stream"/>' /usr/local/freeswitch/conf/autoload_configs/modules.conf.xml
   fi
   ```

5. **退出容器并重启服务**：
   ```bash
   exit
   docker restart outbound_freeswitch
   ```

### 方案3：更新 docker-compose.yml 使用修复后的配置

如果上述方法不起作用，可以更新 docker-compose.yml 以使用一个启动后执行修复脚本的版本：

```yaml
freeswitch:
  image: safarov/freeswitch:latest
  container_name: outbound_freeswitch
  restart: unless-stopped
  # ... 现有配置 ...
  command: >
    sh -c "
    # 修复配置
    sed -i 's/127.0.0.1/0.0.0.0/g' /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml;
    # 创建 audio_stream 配置
    cat > /usr/local/freeswitch/conf/autoload_configs/audio_stream.conf.xml << EOF
<?xml version=\"1.0\" encoding=\"utf-8\"?>
<configuration name=\"audio_stream.conf\" description=\"Real-time audio streaming\">
  <settings>
    <param name=\"default-url\"     value=\"ws://backend:8765\"/>
    <param name=\"default-rate\"    value=\"8000\"/>
    <param name=\"default-ms\"      value=\"20\"/>
    <param name=\"default-channel\" value=\"read\"/>
    <param name=\"connect-timeout\" value=\"3000\"/>
    <param name=\"reconnect-interval-ms\" value=\"1000\"/>
  </settings>
</configuration>
EOF
    ;
    # 确保模块加载
    if ! grep -q \"mod_audio_stream\" /usr/local/freeswitch/conf/autoload_configs/modules.conf.xml; then
        sed -i '/<modules>/a <load module=\"mod_audio_stream\"/>' /usr/local/freeswitch/conf/autoload_configs/modules.conf.xml
    fi;
    # 启动 FreeSWITCH
    exec /usr/local/freeswitch/bin/freeswitch -nonat -nosql -c
    "
  # ... 其余配置 ...
```

## 验证修复

### 1. 检查 ESL 监听状态
```bash
docker exec outbound_freeswitch netstat -tulnp | grep 8021
```
应该显示 `0.0.0.0:8021` 而不是 `127.0.0.1:8021`

### 2. 检查后端连接
```bash
docker exec outbound_backend python3 -c "
import socket
s = socket.socket()
result = s.connect_ex(('freeswitch', 8021))
print('ESL Connection result:', result)
s.close()
"
```
结果应该是 0（成功连接）

### 3. 检查 FreeSWITCH 日志
```bash
docker logs outbound_freeswitch | grep -i "audio_stream"
```
应该不再显示 "cannot open shared object file" 错误

### 4. 检查后端日志
```bash
docker logs outbound_backend | grep -i "websocket\|audio\|8765"
```
确认 WebSocket 服务器正常启动

## Zoiper 注册和通话测试

修复完成后：
1. 重新启动 Zoiper 客户端
2. 使用以下参数注册：
   - 服务器：192.168.5.15
   - 端口：5060
   - 用户名：1001 或 1002
   - 密码：1001 或 1002
3. 测试内部分机通话
4. 测试外呼功能

## AI 外呼功能测试

1. 通过后端 API 发起 AI 外呼
2. 检查后端日志中是否有音频流接收记录
3. 验证 ASR、LLM、TTS 处理链是否正常工作

## 如果问题仍然存在

如果以上修复方法都无法解决问题，可以考虑：

1. **使用预编译的增强镜像**：
   - 搜索社区提供的包含 mod_audio_stream 的 FreeSWITCH 镜像
   - 或者使用 Docker Hub 上的替代镜像

2. **简化方案**：
   - 暂时使用文件轮询方式代替 WebSocket 音频流
   - 后续再解决 mod_audio_stream 模块问题

## 预期结果

修复后，系统应能：
- ✅ ESL 连接正常（后端与 FreeSWITCH 通信）
- ✅ Zoiper 成功注册和通话
- ⚠️ 音频流传输（可能需要完整的 mod_audio_stream 模块才能完全正常工作）
- ✅ AI 外呼基本功能

注意：对于完整的音频流功能，可能仍需要一个包含正确编译的 mod_audio_stream 模块的镜像。