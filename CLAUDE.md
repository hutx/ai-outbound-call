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
- **外部 IP 配置**：`freeswitch/conf/vars.xml` 中 `external_rtp_ip` / `external_sip_ip` 必须设为公网 IP（当前 `61.180.80.84`）

## 架构概览（mod_forkzstream + Qwen ASR + 百炼 TTS）

### 音频流路径
- **sofia A-leg**：用户电话侧（如分机 1001），用户语音从此处进入
- **loopback B-leg**：FreeSWITCH 内部端点，RTP 一定经过软件媒体层
- **bridge(loopback/AI_CALL)**：sofia A-leg ↔ loopback B-leg，RTP 经过 FreeSWITCH 软件媒体层
- **mod_forkzstream**：在 loopback B-leg 上通过 dialplan `forkzstream` 应用推送实时 RTP 音频到后端 8766 端口
- **forkzstream WS**：双向 WebSocket 通道 — ASR 音频（FS → 后端二进制帧）+ TTS 音频（后端 → FS 二进制帧，8kHz L16 PCM）
- **TTS 回传**：`play_stream` 逐 chunk 降采样 16k→8k → forkzstream WS 二进制帧发送到 FS
- **已废弃**：ESL Outbound Socket（audio_stream_ws.py 已删除）、mod_audio_fork、dialplan audio_fork

### 后端模块
- **ForkzstreamWebSocketServer**（`forkzstream_ws.py`，8766 端口）：WebSocket 服务器，接收 FS 音频，推送 TTS
- **ForkzstreamCallSession**（`forkzstream_session.py`）：每个通话的 WS 会话，兼容 CallAgent 接口
- **CallAgent**（`call_agent.py`）：AI 对话引擎，串联 ESL ↔ ASR ↔ LLM ↔ TTS
- **QwenRealtimeASRClient**（`asr_service.py`）：百炼 Qwen 实时语音识别（dashscope SDK OmniRealtimeConversation）
- **BailianTTSClient**（`tts_service.py`）：百炼 CosyVoice TTS（cosyvoice-v3-flash，16kHz PCM）
- **ESL Inbound Pool**（`esl_service.py`）：仅用于通话级控制（originate、uuid_kill、uuid_transfer 等）

### 端口
| 端口 | 用途 |
|------|------|
| 8021 | FreeSWITCH ESL Inbound |
| 8766 | mod_forkzstream WebSocket |
| 8767 | mod_audio_stream WebSocket（保留，非主用） |
| 8000 | FastAPI REST API |

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
  10. ASR 识别（8k→16k 上采样，Qwen qwen3-asr-flash-realtime），返回中间/最终结果
  11. LLM 推理 → TTS 流式播放 → 循环对话
- **关键**：forkzstream WS 使用 callId 作为通话 UUID 标识
- **关键**：TTS 输出 16kHz PCM，`play_stream` 中 `_downsample_16k_to_8k` 降采样后发送
- **关键**：`play_stream` 为流式发送，每收到一个 TTS chunk 就立即发送，不等全部收集
- **端口**：forkzstream WS 监听 8766
- **代码位置**：
  - `backend/services/forkzstream_ws.py` — ForkzstreamWebSocketServer，WS 服务端
  - `backend/services/forkzstream_session.py` — ForkzstreamCallSession，兼容 CallAgent 接口
  - `backend/core/call_agent.py` — CallAgent, AudioStreamAdapter
  - `backend/services/asr_service.py` — QwenRealtimeASRClient, FunASRClient, create_asr_client()
  - `backend/services/tts_service.py` — BailianTTSClient（百炼 CosyVoice）
  - `backend/core/config.py` — 全局配置单例

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

### ✅ barge-in（打断）机制（2026-04-20）
- **双路径检测**：
  - 路径 1（已禁用）：本地 VAD（AudioStreamAdapter.stream()）— 太敏感，ASR 未返回时就触发导致文本为空
  - 路径 2（主用）：Qwen ASR 中间结果（OmniRealtimeConversation）— 实时识别到中文即触发，不等 `is_final`
- **保护期**：`protect_start_ms`（默认 3000ms）内的打断被忽略，防止 TTS 刚开口就被打断
  - ⚠️ **问题**：用户习惯在 AI 播报后 1-2s 内插话，3s 保护期过长导致多数打断被拦截
  - 建议将保护期缩短至 1000-1500ms
- **barge-in 噪声过滤**：与主路径不同，使用 **宽松版过滤** — 只丢弃纯英文（"ok", "system"），**不丢弃**中文语气词（"嗯"、"好"、"对"）。打断场景下用户说任何中文都表示在说话。
- **不丢弃 RMS 能量过低**：barge-in 场景下即使 RMS 低，ASR 识别到中文就是有效打断。
- **TTS 播放完不取消 barge-in ASR**：让它在 TTS→listen 空窗期继续监听用户语音，填补间隙。
- **_listen_user() 启动时取消残留 barge-in ASR**：防止两个 ASR 同时运行。
- **文本携带**：barge-in 触发时保存识别文本到 `_barge_in_text`，`_say()` 返回给主循环，用该文本走 LLM。
- **代码位置**：`backend/core/call_agent.py` — `_say()` + `_barge_in_asr_loop_with_queue()` + `_is_noise()`（宽松版）

### ✅ Qwen ASR 噪声过滤（2026-04-20 更新）
Qwen ASR 会将背景噪音识别为语气词（"嗯。"、"你好。"等），需 **多重过滤**：

- **过滤 1：噪声词集合 + 模式匹配**
  - 基础噪声词：`{"嗯", "嗯。", "哦", "哦。", "啊", "啊。", "呃", "呃。", "哎", "哎。", "你好", "你好。", "你", "你。", "对", "对。", "行", "行。", "喂", "喂。", "好", "好。", "是", "是。"}`
  - 重复语气词：`"嗯嗯"`, `"好好"`, `"对对"`, `"哦哦"` + 可选标点
  - 常见短语：`"就是"`, `"好吧"`, `"没事"`, `"嗯，对吧。"`, `"嗯，可以。"`
  - 常见英文：`"ok"`, `"yes"`, `"no"`, `"system"` + 可选标点
  - 规则：纯英文单词（无中文，长度 < 10）→ 噪声；重复字符（同一字符 ≥2 次）→ 噪声；逗号分隔的语气词组合 → 噪声；单字 + 标点 → 噪声

- **过滤 2：RMS 能量过滤**
  - `_max_rms < 1500` → 判定为环境噪音（真实语音 RMS > 1500，噪音 RMS < 1000）
  - 使用 `_max_rms`（全局峰值）而非 `_seg_max_rms`（段级峰值），因为 `server_vad_mode=True` 时本地 VAD 不参与语音判定，段级追踪无效
  - **重要**：每次 `_listen_user()` 会创建新的 Qwen ASR WebSocket 连接，不是长连接

- **应用路径**：
  - 主路径：`_listen_user()` 中 ASR `is_final=True` 结果先经噪声词过滤，再经 RMS 过滤
  - 宽容期路径：`_listen_tolerance()` 同样应用双重过滤
  - barge-in 路径：`_barge_in_asr_loop_with_queue` 使用 **宽松版过滤**（只丢弃纯英文，不丢弃语气词，不检查 RMS）

- **代码位置**：`backend/core/call_agent.py` — `_listen_user()` 内 `_is_noise_word()` 函数（主路径）+ `_barge_in_asr_loop_with_queue()` 内 `_is_noise()`（barge-in 宽松版）

### ✅ Qwen ASR OmniRealtimeConversation（dashscope SDK，2026-04-19）
- **必须使用** `enable_turn_detection=False`，否则模型进入"对话模式"（生成 AI 回复）而非纯转录
- **URL**：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime`
- **模型**：`qwen3-asr-flash-realtime`（或 `qwen3-asr-flash-realtime-2026-02-10`）
- **音频格式**：PCM 16-bit，发送端 16kHz（8kHz 需线性插值上采样）
- **事件类型**：`conversation.item.input_audio_transcription.text`（中间结果）、`conversation.item.input_audio_transcription.completed`（最终结果）
- **代码位置**：`backend/services/asr_service.py` — `QwenRealtimeASRClient`

### ✅ ASR 静音检测（VAD 层）
- `AudioStreamAdapter._is_all_silent`：stream() 退出时设置，`_started_speaking=False` 表示全程静音
- `_conversation_loop` 中 `is_all_silent=True` 时不计入 silence_retry，防止误挂断
- **日期**：2026-04-18

### ✅ 百炼 TTS 采样率
- TTS 输出格式：`PCM_16000HZ_MONO_16BIT`（提升音质）
- `play_stream` 中 `_downsample_16k_to_8k` 降采样为 8kHz 后发给 forkzstream/FreeSWITCH
- 流式发送：逐 chunk 立即发送，不等全部收集完
- **日期**：2026-04-18

### ✅ ASR 兜底策略（带噪声过滤）
- 如果 ASR 未产出 `is_final=True` 结果但有人声 + 中间结果，使用最后一条中间结果
- **多重过滤**：
  1. `max_rms > 2000` → 跳过（TTS 回声）
  2. `speech_ms < 200` → 跳过（噪音）
  3. `_is_noise_word(text)` → 跳过（噪声词）
- **日期**：2026-04-19

### ✅ 连续语音帧检测
- 需连续 2 帧语音（40ms）才认为用户开始说话，防止开头噪音误触发
- **代码位置**：`backend/core/call_agent.py` — `AudioStreamAdapter._speech_onset_threshold = 2`

### ✅ VAD 静音超时
- `vad_silence_ms = 400~500`（config 默认值），运行时环境变量 `VAD_SILENCE_MS` 可覆盖
- `energy_threshold=120`（音频能量较低，需确保能触发语音检测）
- **代码位置**：`backend/core/config.py`, `backend/core/call_agent.py`

### ✅ ASR/LLM/TTS 超时配置
- `ASR_TIMEOUT = 15.0`（单句最长等待，百炼流式 ASR 首包约 2-5s）
- `LLM_TIMEOUT = 30.0`
- `TTS_TIMEOUT = 10.0`

### ✅ forkzstream WS 断开支路
- WS 断开时向订阅队列发送空 bytes sentinel，AudioStreamAdapter 检测到立即退出
- 避免 WS 断后空等 15s 超时
- **代码位置**：`backend/services/forkzstream_ws.py` — `_handle_connection` finally 块

### ✅ 调试音频自动保存
- `AudioStreamAdapter.stream()` 将送入 ASR 的 PCM 音频保存到 `/recordings/debug/asr_input_*.wav`
- 便于追溯 ASR 识别质量、VAD 检测准确性
- **代码位置**：`backend/core/call_agent.py` — `AudioStreamAdapter.stream()` 内 dump 逻辑

### ✅ 先发后改策略（2026-04-20）
- **`_listen_user()`**：收到第一个有效文本后立即返回，不等待宽容期
- **`_conversation_loop()`**：先发 → 立即启动 LLM 任务；后改 → 并行启动 `_listen_tolerance()` 宽容期监听
- **`_listen_tolerance()`**：宽容期内如果收到新的非噪声有效文本，取消 LLM 任务，合并文本后重新调用
- **并行机制**：`asyncio.wait([llm_task, tolerance_task], return_when=asyncio.FIRST_COMPLETED)`
- **配置来源**：`tolerance_enabled` 和 `tolerance_ms` 从话术脚本的 barge-in 配置中读取
- **代码位置**：`backend/core/call_agent.py` — `_conversation_loop()`, `_listen_user()`, `_listen_tolerance()`

### ✅ Qwen ASR 连接生命周期（2026-04-20）
- 每次 `_listen_user()` 调用会 **创建新的 Qwen ASR WebSocket 连接**，不是长连接
- 连接生命周期：创建 session → 发送音频 → 接收结果 → 超时/识别到文本 → 关闭
- `server_vad_mode=True` 时，ASR 超时是因为 15s 内没有收到过滤后的有效 `is_final` 结果
- 中间结果（`is_final=False`）被 `_listen_user()` 忽略，只有 `is_final=True` 才会被消费
- **日志特征**：每次 `session created` 的 id 都不同（`sess_Qw4B8...`, `sess_OjdkB...` 等）

### ✅ barge-in ASR 在 TTS 播放完成后不取消（2026-04-20）
- **之前**：`_say()` 在 TTS 播放完成后调用 `barge_in_adapter.stop()` + `await barge_in_asr_task`，barge-in ASR 立即退出
- **现在**：TTS 播放完成后不取消 barge-in ASR，让它在 TTS→listen 空窗期继续监听
- `_listen_user()` 启动时通过 `self._barge_in_asr_task` 检查并取消残留任务，防止两个 ASR 同时运行
- **根因**：用户在 TTS 播完后才开始说话，barge-in ASR 已退出导致语音积累在队列，被 `_listen_user()` 捕获（表现为"没打断"）

## 项目结构
- `backend/` - Python FastAPI 后端
  - `api/` - REST API 路由
  - `core/` - 核心业务逻辑
    - `call_agent.py` — CallAgent, AudioStreamAdapter
    - `state_machine.py` — 通话状态机
    - `config.py` — 全局配置单例
  - `services/` - 外部服务
    - `forkzstream_ws.py` — ForkzstreamWebSocketServer（8766 端口）
    - `forkzstream_session.py` — ForkzstreamCallSession
    - `asr_service.py` — ASR 服务（QwenRealtimeASRClient, FunASRClient, BaseASR）
    - `tts_service.py` — TTS 服务（百炼 CosyVoice）
    - `esl_service.py` — ESL Inbound 连接池
- `freeswitch/conf/` - FreeSWITCH 配置（dialplan, autoload_configs, sip_profiles）
- `freeswitch/mod/` - FreeSWITCH 模块（mod_forkzstream.so）
- `docker/` - Docker Compose 部署配置
- `frontend/` - 管理界面前端
- `tests/` - 测试
