# FreeSWITCH AI 增强版构建说明

## 问题诊断总结

通过分析日志，发现以下关键问题：

1. **mod_audio_stream 模块缺失**：
   ```
   [CRIT] Error Loading module /usr/lib/freeswitch/mod/mod_audio_stream.so
   **/usr/lib/freeswitch/mod/mod_audio_stream.so: cannot open shared object file: No such file or directory**
   ```

2. **ESL 监听问题**：ESL 服务只监听在 127.0.0.1 而非 0.0.0.0

3. **音频流传输中断**：由于缺少 mod_audio_stream 模块，无法将音频流推送到后端 WebSocket 服务

## 解决方案

### 1. 自定义 Dockerfile (Dockerfile.freeswitch-ai)

构建一个带有 mod_audio_stream 模块的 FreeSWITCH 镜像：

```dockerfile
FROM safarov/freeswitch:latest

# 安装构建依赖
RUN apt-get update && \
    apt-get install -y \
    build-essential \
    git \
    libtool \
    autoconf \
    automake \
    pkg-config \
    libcurl4-openssl-dev \
    libpcre3-dev \
    libssl-dev \
    libldns-dev \
    libedit-dev \
    libpq-dev \
    unixodbc-dev \
    libsqlite3-dev \
    libspeex-dev \
    libspeexdsp-dev \
    libg722-dev \
    libopus-dev \
    libsndfile1-dev \
    libjpeg-dev \
    libmemcached-dev \
    libavformat-dev \
    libswscale-dev \
    libavresample-dev \
    libavutil-dev \
    libswresample-dev \
    libmp4v2-dev \
    libyuv-dev \
    libvpx-dev \
    libtheora-dev \
    libvorbis-dev \
    libflite-dev \
    libmpg123-dev \
    libtiff5-dev \
    libhiredis-dev \
    libks-libkurento-client-dev \
    wget \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 创建源码目录
WORKDIR /usr/src

# 克隆 FreeSWITCH 源码
RUN git clone https://github.com/signalwire/freeswitch.git && \
    cd freeswitch && \
    git checkout v1.10.10

# 克隆 mod_audio_stream 模块
RUN git clone https://github.com/nicholasinatel/mod_audio_stream.git && \
    cp -r mod_audio_stream /usr/src/freeswitch/src/mod/applications/

# 配置和构建 FreeSWITCH
WORKDIR /usr/src/freeswitch
RUN ./bootstrap.sh && \
    ./configure && \
    make mod_audio_stream-all && \
    make mod_audio_stream-install

# 验证模块安装
RUN ls -la /usr/local/freeswitch/mod/ | grep audio_stream

# 创建自定义启动脚本
RUN echo '#!/bin/bash\n\
echo "Starting FreeSWITCH with mod_audio_stream support..."\n\
echo "Checking if mod_audio_stream is available:"\n\
ls -la /usr/local/freeswitch/mod/ | grep audio_stream || echo "Warning: mod_audio_stream not found!"\n\
echo "Checking event socket configuration:"\n\
cat /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml | grep -A 2 -B 2 listen\n\
exec /usr/local/freeswitch/bin/freeswitch -nonat -nosql -c -nonat -nosql -c -rp\n\
' > /usr/local/bin/start-freeswitch.sh && \
chmod +x /usr/local/bin/start-freeswitch.sh

# 暴露必要端口
EXPOSE 8021 8022 5060 5060/udp 5080 5080/udp 16384-17384/udp

# 使用自定义启动脚本
CMD ["/usr/local/bin/start-freeswitch.sh"]
```

### 2. 构建和使用说明

1. **构建镜像**：
   ```bash
   cd /Users/hutx/work/hvyogo/ai_work/ai-outbound-call-new
   docker build -f docker/Dockerfile.freeswitch-ai -t freeswitch-ai-enhanced:latest .
   ```

2. **更新 docker-compose.yml**：
   ```yaml
   freeswitch:
     image: freeswitch-ai-enhanced:latest
     # ... 其他配置保持不变
   ```

3. **验证构建**：
   ```bash
   # 检查模块是否安装
   docker run --rm freeswitch-ai-enhanced:latest ls -la /usr/local/freeswitch/mod/ | grep audio_stream
   
   # 检查 ESL 配置
   docker run --rm freeswitch-ai-enhanced:latest cat /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml | grep listen
   ```

### 3. 网络配置修复

确保 event_socket 配置正确：

```xml
<configuration name="event_socket.conf" description="Socket Client">
  <settings>
    <param name="listen-ip" value="0.0.0.0"/>  <!-- 修复：监听所有接口 -->
    <param name="listen-port" value="8021"/>
    <param name="password" value="$${default_password}"/>
  </settings>
</configuration>
```

### 4. 音频流配置

确保 audio_stream 配置正确：

```xml
<configuration name="audio_stream.conf" description="Real-time audio streaming">
  <settings>
    <param name="default-url"     value="ws://backend:8765"/>
    <param name="default-rate"    value="8000"/>
    <param name="default-ms"      value="20"/>
    <param name="default-channel" value="read"/>
  </settings>
</configuration>
```

### 5. Zoiper 配置

根据您提供的信息，Zoiper 的 IP 是 192.168.5.22，主机 IP 是 192.168.5.15：

- 服务器地址：192.168.5.15
- 端口：5060
- 用户名：1001/1002（根据您的配置）
- 密码：1001/1002（根据您的配置）

### 6. 拨号计划验证

确保拨号计划正确配置了内部通话和外呼功能：

- internal.xml: 内部分机直拨（1001↔1002）
- outbound.xml: 外呼路由
- ai_outbound.xml: AI 外呼处理

### 7. 启动顺序

1. 启动数据库服务
2. 启动 FreeSWITCH 服务
3. 启动后端服务
4. 验证服务连接

## 验证步骤

1. **检查模块加载**：
   ```bash
   docker logs <freeswitch-container> | grep -i "audio_stream"
   ```

2. **检查端口监听**：
   ```bash
   docker exec <freeswitch-container> netstat -tulnp | grep 8021
   # 应该显示 0.0.0.0:8021 而不是 127.0.0.1:8021
   ```

3. **测试连接**：
   ```bash
   docker exec <backend-container> python3 -c "
   import socket
   s = socket.socket()
   result = s.connect_ex(('freeswitch', 8021))
   print('ESL Connection result:', result)
   s.close()
   "
   ```

4. **测试 Zoiper 注册和通话**

5. **测试 AI 外呼功能**

## 预期结果

构建完成后，新的 FreeSWITCH 镜像将：
- ✅ 包含 mod_audio_stream 模块
- ✅ ESL 服务监听在 0.0.0.0:8021
- ✅ 能够将音频流推送到后端 WebSocket 服务
- ✅ 支持 Zoiper 注册和通话
- ✅ 支持 AI 外呼功能