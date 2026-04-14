# CLAUDE.md

## 语言偏好
所有回复使用中文。

## 开发环境部署
- **后端代码**：通过 Docker 卷映射（`./backend:/app/backend`），修改代码后 `docker compose restart backend` 即可生效，无需 rebuild
- **FreeSWITCH 配置**：通过 Docker 卷映射（`./freeswitch/conf/:/etc/freeswitch/` 部分挂载），修改配置后需执行 `docker exec outbound_freeswitch1 fs_cli -x "reloadxml"` 重载
- **Docker Compose 配置**：`docker/docker-compose.yml`
- **FreeSWITCH 日志**：`docker logs outbound_freeswitch1`
- **FreeSWITCH 容器名**：`outbound_freeswitch1`
- **后端日志**：`docker logs outbound_backend`

## 架构概览
- **sofia A-leg**：用户电话侧（如分机 1001），用户语音从此处进入
- **loopback B-leg**：FreeSWITCH 内部端点，RTP 一定经过软件媒体层
- **bridge(loopback/AI_CALL)**：sofia A-leg ↔ loopback B-leg，RTP 经过 FreeSWITCH 软件媒体层
- **mod_audio_stream**：通过 WebSocket 推送实时 RTP 音频到后端 8765 端口
- **socket(async full)**：ESL Outbound socket 接管媒体路径，TTS 音频能正确播放到用户电话

## 已验证失败的方案（不要再试）

### 1. ❌ &park() + uuid_transfer 到 AI_Handler (9998)
- **命令**：`originate {vars}user/1002@domain &park()` → `uuid_transfer 9998 XML internal`
- **问题**：通道卡在 `CS_EXECUTE` 状态，dialplan 不继续执行
- **现象**：用户能听到振铃但 AI 语音播报无法播放
- **日期**：2026-04-14

### 2. ❌ dialplan audio_stream 应用在 socket 之前
- **命令**：`<action application="audio_stream" data="ws://backend:8765/${uuid} mono 8000"/>` 在 socket 之前
- **问题**：audio_stream 应用阻塞 dialplan 执行，后续 socket 应用永远无法运行
- **现象**：通道卡在 `CS_EXECUTE` 状态，TTS 无法播放
- **日期**：2026-04-14

### 3. ❌ socket(async) 不用 full
- **命令**：`<action application="socket" data="backend:9999 async"/>`
- **问题**：`async` 模式下 socket 不接管媒体路径，uuid_broadcast TTS 无法送达
- **现象**：TTS 播放返回 +OK 但用户听不到声音
- **日期**：2026-04-14

### 4. ❌ audio_stream 挂载在 loopback B-leg
- **命令**：在 loopback B-leg（CallAgent socket 通道）上执行 `uuid_audio_stream` 或 dialplan `audio_stream`
- **问题**：loopback B-leg 的 read 方向在 bridge(sofia ↔ loopback) 中捕获不到 sofia 侧的用户语音
- **现象**：WebSocket 收到完整音频帧（2704 帧 / 54 秒），但全是静音（max_rms=0），ASR 无法识别
- **注意**：与 Zoiper 编解码无关，编解码不匹配会导致呼叫建立失败而非静音
- **日期**：2026-04-14

### 5. ❌ sofia profile 级 proxy-media 参数不生效
- **配置**：`<param name="proxy-media" value="true"/>` 在 sofia profile settings 中
- **问题**：FreeSWITCH 重启后仍显示 `proxy-media=false`，该参数被静默忽略
- **注意**：`proxy-media` 是 channel variable，不是 profile 级参数
- **日期**：2026-04-15

## 当前外呼策略（已验证有效）

### 内部分机：bridge(loopback/AI_CALL) + sofia A-leg uuid_audio_stream + CallAgent uuid_displace
- **命令**：`originate [{vars},proxy_media=true] user/1002@domain &bridge(loopback/AI_CALL)`
- **流程**：
  1. originate 创建 sofia A-leg（用户电话侧）
  2. `proxy_media=true` 确保 RTP 经过 FreeSWITCH 软件层，不旁路
  3. `&bridge(loopback/AI_CALL)` 桥接到 loopback B-leg
  4. loopback B-leg 匹配 default.xml 的 `ai_call_handler` 扩展
  5. dialplan 执行：answer → sleep → record_session → socket(async full)
  6. socket 连接后端 ESL Outbound → CallAgent.run()
  7. CallAgent._discover_aleg_uuid() → _start_audio_stream()
  8. `uuid_audio_stream {sofia_a_leg_uuid} start ws://backend:8765/{stream_uuid} mono 8000`
  9. FreeSWITCH 通过 mod_audio_stream 将 RTP 音频推送到 WebSocket
  10. 后端 WebSocket 服务接收音频 → 广播到 ASR 订阅队列
  11. ASR WebSocket 推送识别结果 → CallAgent 处理
  12. CallAgent._say_opening() → _say() → TTS 生成 → `uuid_displace` 写入 sofia A-leg
  13. 后续 TTS 同样通过 `uuid_displace` + `execute playback` 播放到通道
- **关键**：loopback 不是 sofia 端点，bridge 后 RTP 不会旁路
- **关键**：`proxy_media=true` 必须在 sofia A-leg 的 originate 命令中设置，否则 RTP 旁路导致 audio_stream 只推静音
- **关键**：`uuid_audio_stream` 的 WebSocket URL 必须使用 **stream_uuid**（sofia A-leg UUID），不能用 B-leg UUID
  - FreeSWITCH 通过 URL 中的 UUID 标识 WebSocket 连接
  - 后端查找 WebSocket 队列也必须用同一个 UUID
- **`uuid_displace` 正确目标查找**（`esl_service.py` `_discover_aleg_uuid()` + `play()`）：
  - 优先级：`other_loopback_from_uuid` > `export_origination_uuid` > `origination_uuid` > `signal_bond` > `other_loopback_leg_uuid`
  - `other_loopback_from_uuid` = sofia A-leg UUID ✓（正确目标）
  - `other_loopback_leg_uuid` = loopback-a UUID ✗（写到它用户听不到）
- **首次播放延迟**：`play()` 中首次 `uuid_displace` 等待 3 秒（socket 接管媒体路径需要时间），后续播放等待 2 秒
- **降级方案**：文件轮询 — 读取 `export_origination_uuid` 对应的 record_session WAV 文件
- **代码位置**：`backend/services/esl_service.py` — `ESLPool.originate()`, `ESLSocketCallSession.start_audio_capture()`, `_discover_aleg_uuid()`, `_poll_audio_file()`

### PSTN 外呼：&park() + dialplan socket
- **命令**：`originate [{vars}]sofia/gateway/... &park()`
- **流程**：PSTN 接听后匹配 `ai_outbound_bleg` → socket(async full) → CallAgent 通过 uuid_displace 播放 TTS

## 关键发现

### ❌ ESL bgapi originate 命令中 `}` 和端点之间不能有空格
- **错误格式**：`originate {vars} user/1001@...`（有空格）
- **正确格式**：`originate [{vars}] user/1001@...`（`[]` 语法允许空格）
- **日期**：2026-04-13

### ❌ 变量语法：`{}` vs `[]`
- `{var=val}` — FreeSWITCH 变量，紧跟端点不能有空格
- `[var=val]` — channel 变量，允许端点前有空格

### ✅ `proxy_media=true` 必须在 originate 命令中设置
- **问题**：sofia A-leg 的 RTP 默认旁路（P2P），`uuid_audio_stream` 捕获不到用户语音
- **表现**：WebSocket 连接成功但只收到 1 帧静音（320 bytes, max_rms=0）
- **修复**：`originate [{vars},proxy_media=true] user/1002@domain &bridge(loopback/AI_CALL)`
- **验证**：`max_rms=12679 total_chunks=160 speech_detected=True`
- **日期**：2026-04-15

### ✅ `uuid_displace` 必须写到 sofia A-leg UUID
- **问题**：loopback bridge 后有两个相关 UUID：
  - `other_loopback_leg_uuid` = loopback-a（FreeSWITCH 内部端点，写到它用户听不到）
  - `other_loopback_from_uuid` = sofia A-leg（用户电话侧，写到它用户才能听到）
- **验证**：`uuid_displace(loopback-a)` 返回 +OK 但用户听不到；`uuid_displace(sofia A-leg)` 用户能听到
- **日期**：2026-04-14

### ✅ `uuid_audio_stream` WebSocket URL 必须使用 stream_uuid
- **问题**：WebSocket URL 使用 B-leg UUID，FreeSWITCH 连接标识不匹配，后端收不到音频
- **修复**：`ws://backend:8765/{stream_uuid}`（stream_uuid = sofia A-leg UUID）
- **日期**：2026-04-15

## 项目结构
- `backend/` - Python FastAPI 后端
  - `api/` - REST API 路由
  - `core/` - 核心业务逻辑（CallAgent, TaskScheduler, 状态机）
  - `services/` - 外部服务（ESL, ASR, TTS, LLM）
- `freeswitch/conf/` - FreeSWITCH 配置（dialplan, autoload_configs, sip_profiles）
- `docker/` - Docker Compose 部署配置
- `frontend/` - 管理界面前端
