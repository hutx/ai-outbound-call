# CLAUDE.md

## 语言偏好
所有回复使用中文。

## 开发环境部署
- **后端代码**：通过 Docker 卷映射（`./backend:/app/backend`），修改代码后 `docker compose restart backend` 即可生效，无需 rebuild
- **FreeSWITCH 配置**：通过 Docker 卷映射（`./freeswitch/conf/:/usr/local/freeswitch/conf/` 部分挂载），修改配置后需执行 `docker exec outbound_freeswitch fs_cli -x "reloadxml"` 重载
- **Docker Compose 配置**：`docker/docker-compose.yml`
- **FreeSWITCH 日志**：`docker logs outbound_freeswitch`
- **FreeSWITCH 容器名**：`outbound_freeswitch`
- **后端日志**：`docker logs outbound_backend`

## 架构概览
- **sofia A-leg**：用户电话侧（如分机 1001），用户语音从此处进入
- **loopback B-leg**：FreeSWITCH 内部端点，RTP 一定经过软件媒体层
- **bridge(loopback/AI_CALL)**：sofia A-leg ↔ loopback B-leg，RTP 经过 FreeSWITCH 软件媒体层
- **mod_forkzstream**：替代 mod_audio_fork，通过 WebSocket 推送实时 RTP 音频到后端 8766 端口
- **forkzstream WS**：双向通道 — ASR 音频（FS → 后端二进制帧）+ TTS 音频（后端 → FS 二进制帧，8kHz L16 PCM）
- **已废弃**：ESL Outbound Socket（audio_stream_ws.py 已删除）

## 已验证失败的方案（不要再试）

### 1. ❌ &park() + uuid_transfer 到 AI_Handler (9998)
- **命令**：`originate {vars}user/1002@domain &park()` → `uuid_transfer 9998 XML internal`
- **问题**：通道卡在 `CS_EXECUTE` 状态，dialplan 不继续执行
- **日期**：2026-04-14

### 2. ❌ dialplan audio_fork 应用
- **命令**：`<action application="audio_fork" data="ws://..."/>`
- **问题**：mod_audio_fork **不提供** dialplan application，只提供 `uuid_audio_fork` API 命令
- **日期**：2026-04-15

### 3. ❌ socket(async) 不用 full
- **命令**：`<action application="socket" data="backend:9999 async"/>`
- **问题**：`async` 模式下 socket 不接管媒体路径，uuid_broadcast TTS 无法送达
- **日期**：2026-04-14

### 4. ❌ audio_stream 挂载在 loopback B-leg
- **问题**：loopback B-leg 的 read 方向在 bridge(sofia ↔ loopback) 中捕获不到 sofia 侧的用户语音
- **现象**：WebSocket 收到完整音频帧但全是静音（max_rms=0）
- **日期**：2026-04-14

### 5. ❌ sofia profile 级 proxy-media 参数不生效
- **注意**：`proxy-media` 是 channel variable，不是 profile 级参数
- **日期**：2026-04-15

### 6. ❌ mod_audio_fork 在 sofia A-leg 上推送静音
- **结论**：`proxy_media=true` 能确保 RTP 经过 FreeSWITCH 软件层，但 mod_audio_fork 的 media bug 在 sofia leg 上就是捕获不到音频帧
- **替代方案**：mod_forkzstream 替代 mod_audio_fork（已验证有效）

### 7. ✅ Docker RTP 端口映射限制 — 已修复
- **修复**：`vars.xml` 中 `rtp_start_port=16384` + `rtp_end_port=17384`，docker-compose 端口映射 `"16384-17384:16384-17384/udp"`
- **日期**：2026-04-15

### 8. ❌ drachtio/drachtio-freeswitch-mrf 镜像不适用
- **原因**：容器启动时 entrypoint 的 sed 命令在只读挂载卷上失败
- **日期**：2026-04-15

## 当前外呼策略（已验证有效）

### forkzstream 架构（主方案）
- **命令**：`originate [{vars},proxy_media=true] user/1002@domain &bridge(loopback/AI_CALL)`
- **流程**：
  1. originate 创建 sofia A-leg（用户电话侧）
  2. `proxy_media=true` 确保 RTP 经过 FreeSWITCH 软件层
  3. `&bridge(loopback/AI_CALL)` 桥接到 loopback B-leg
  4. loopback B-leg 匹配 dialplan 的 `ai_call_handler` 扩展
  5. dialplan 执行：answer → sleep → record_session → forkzstream
  6. forkzstream WebSocket 连接后端 8766 端口 → 自动握手 → 启动 CallAgent
  7. CallAgent 通过 `session.start_audio_capture()` 获取 forkzstream 音频队列
  8. ASR 音频：forkzstream WS → ForkzstreamCallSession → AudioStreamAdapter → ASR
  9. TTS 音频：百炼 CosyVoice (16kHz) → `play_stream` 逐 chunk 降采样到 8kHz → forkzstream WS 发送
  10. ASR 识别（8k→16k 上采样，百炼 paraformer-realtime-v1），返回中间/最终结果
  11. LLM 推理 → TTS 流式播放 → 循环对话
- **关键**：forkzstream WS 使用 callId 作为通话 UUID 标识
- **关键**：TTS 输出 16kHz PCM，`play_stream` 中 `_downsample_16k_to_8k` 降采样后发送
- **关键**：`play_stream` 为流式发送，每收到一个 TTS chunk 就立即发送，不等全部收集
- **端口**：forkzstream WS 监听 8766
- **代码位置**：
  - `backend/services/forkzstream_ws.py` — ForkzstreamWebSocketServer，WS 服务端
  - `backend/services/forkzstream_session.py` — ForkzstreamCallSession，兼容 CallAgent 接口
  - `backend/core/call_agent.py` — CallAgent, AudioStreamAdapter

### PSTN 外呼：&park() + dialplan forkzstream
- **命令**：`originate [{vars}]sofia/gateway/... &park()`
- **流程**：PSTN 接听后匹配 dialplan → forkzstream → CallAgent 通过 play_stream 播放 TTS

## 关键发现

### ❌ ESL bgapi originate 命令中 `}` 和端点之间不能有空格
- **错误格式**：`originate {vars} user/1001@...`（有空格）
- **正确格式**：`originate [{vars}] user/1001@...`（`[]` 语法允许空格）
- **日期**：2026-04-13

### ❌ 变量语法：`{}` vs `[]`
- `{var=val}` — FreeSWITCH 变量，紧跟端点不能有空格
- `[var=val]` — channel 变量，允许端点前有空格

### ✅ `uuid_displace` 必须写到 sofia A-leg UUID
- `other_loopback_from_uuid` = sofia A-leg UUID ✓（正确目标）
- `other_loopback_leg_uuid` = loopback-a UUID ✗（写到它用户听不到）
- **日期**：2026-04-14

### ✅ ASR 静音检测（VAD 层）
- `AudioStreamAdapter._is_all_silent`：stream() 退出时设置，`_started_speaking=False` 表示全程静音
- `_conversation_loop` 中 `is_all_silent=True` 时不计入 silence_retry，防止误挂断
- **日期**：2026-04-18

### ✅ 百炼 TTS 采样率
- TTS 输出格式：`PCM_16000HZ_MONO_16BIT`（提升音质）
- `play_stream` 中 `_downsample_16k_to_8k` 降采样为 8kHz 后发给 forkzstream/FreeSWITCH
- 流式发送：逐 chunk 立即发送，不等全部收集完
- **日期**：2026-04-18

### ✅ ASR 兜底策略
- 如果 ASR 未产出 `is_final=True` 结果但有人声 + 中间结果，使用最后一条中间结果
- **日期**：2026-04-15

### ✅ 连续语音帧检测
- 需连续 2 帧语音（40ms）才认为用户开始说话，防止开头噪音误触发
- **代码位置**：`backend/core/call_agent.py` — `AudioStreamAdapter._speech_onset_threshold = 2`

### ✅ VAD 静音超时
- `vad_silence_ms = 400`（config 默认值），运行时环境变量 `VAD_SILENCE_MS` 可覆盖
- `energy_threshold=120`（文件轮询音频能量较低）
- **代码位置**：`backend/core/config.py`, `backend/core/call_agent.py`

### ✅ ASR 超时
- `ASR_TIMEOUT = 8.0`（单句最长等待）
- `LLM_TIMEOUT = 30.0`
- `TTS_TIMEOUT = 10.0`

### ✅ forkzstream WS 断开支路
- WS 断开时向订阅队列发送空 bytes sentinel，AudioStreamAdapter 检测到立即退出
- 避免 WS 断后空等 15s 超时
- **代码位置**：`backend/services/forkzstream_ws.py` — `_handle_connection` finally 块

## 项目结构
- `backend/` - Python FastAPI 后端
  - `api/` - REST API 路由
  - `core/` - 核心业务逻辑（CallAgent, TaskScheduler, 状态机）
  - `services/` - 外部服务（forkzstream, ASR, TTS, LLM, CRM）
- `freeswitch/conf/` - FreeSWITCH 配置（dialplan, autoload_configs, sip_profiles）
- `freeswitch/mod/` - FreeSWITCH 模块（mod_forkzstream.so）
- `docker/` - Docker Compose 部署配置
- `frontend/` - 管理界面前端
