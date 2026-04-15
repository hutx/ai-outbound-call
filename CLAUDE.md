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
- **mod_audio_fork**：通过 WebSocket 推送实时 RTP 音频到后端 8765 端口（替代 mod_audio_stream）
- **socket(async full)**：ESL Outbound socket 接管媒体路径，TTS 音频能正确播放到用户电话

## 已验证失败的方案（不要再试）

### 1. ❌ &park() + uuid_transfer 到 AI_Handler (9998)
- **命令**：`originate {vars}user/1002@domain &park()` → `uuid_transfer 9998 XML internal`
- **问题**：通道卡在 `CS_EXECUTE` 状态，dialplan 不继续执行
- **现象**：用户能听到振铃但 AI 语音播报无法播放
- **日期**：2026-04-14

### 2. ❌ dialplan audio_fork 应用
- **命令**：`<action application="audio_fork" data="ws://..."/>`
- **问题**：mod_audio_fork **不提供** dialplan application，只提供 `uuid_audio_fork` API 命令
- **现象**：FreeSWITCH 返回 "Invalid Application audio_fork"
- **日期**：2026-04-15

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

### 6. ❌ mod_audio_fork 在 sofia A-leg 上推送静音（proxy_media 无法解决）
- **命令**：`uuid_audio_fork {sofia_a_leg_uuid} start ws://backend:8765/{call_uuid} mono 8000`
- **问题**：WebSocket 收到完整帧（3700+ 帧），但全是 `ff ff ff` 静音字节（max_rms=2）
- **原因**：sofia A-leg RTP 旁路（P2P），即使 `proxy_media=true` 在 originate 和 dialplan 中都确认设置成功（`uuid_dump` 显示 `variable_proxy_media: true`），mod_audio_fork 的 media bug 仍捕获不到有效 RTP
- **验证日期**：2026-04-15（多次测试，包括在 internal.xml Local_Extension 中添加 proxy_media=true 后仍然失败）
- **结论**：`proxy_media=true` 能确保 RTP 经过 FreeSWITCH 软件层（bridge 正常），但 mod_audio_fork 的 media bug 在 sofia leg 上就是捕获不到音频帧 — 这是 mod_audio_fork 的限制
- **降级方案**：文件轮询 — 读取 `record_session` 的 WAV 文件，跳过 44 字节 WAV 头，320 字节分帧送入 ASR（已验证有效）

### 7. ✅ Docker RTP 端口映射限制 — 已修复
- **配置**：`docker-compose.yml` 中 `"16384-32768:16384-32768/udp"`（16385 个端口）
- **问题**：Docker Desktop (Mac) 实际只映射了约 1000 个 UDP 端口（16384-17384），超出范围的端口无法到达容器
- **现象**：FreeSWITCH 随机选择到 17385+ 的 RTP 端口时（如 19164），Zoiper 的 RTP 无法到达 FreeSWITCH
- **修复**：
  1. `vars.xml` 中 `rtp_start_port=16384` + `rtp_end_port=17384`
  2. `docker-compose.yml` 中端口映射改为 `"16384-17384:16384-17384/udp"`
- **日期**：2026-04-15

### 8. ❌ drachtio/drachtio-freeswitch-mrf 镜像不适用
- **原因**：容器启动时 entrypoint 的 sed 命令在只读挂载卷上失败（`Device or resource busy`）
- **现象**：容器退出码 137
- **日期**：2026-04-15

## 当前外呼策略（已验证有效）

### 内部分机：bridge(loopback/AI_CALL) + 文件轮询 ASR + CallAgent uuid_displace
- **命令**：`originate [{vars},proxy_media=true] user/1002@domain &bridge(loopback/AI_CALL)`
- **流程**：
  1. originate 创建 sofia A-leg（用户电话侧）
  2. `proxy_media=true` 确保 RTP 经过 FreeSWITCH 软件层，不旁路
  3. `&bridge(loopback/AI_CALL)` 桥接到 loopback B-leg
  4. loopback B-leg 匹配 default.xml 的 `ai_call_handler` 扩展
  5. dialplan 执行：answer → sleep → record_session → socket(async full)
  6. socket 连接后端 ESL Outbound → CallAgent.run()
  7. CallAgent._discover_aleg_uuid() → start_audio_capture()
  8. mod_audio_fork 尝试在 sofia A-leg 上推送（当前返回静音，但 WebSocket 连接成功）
  9. start_audio_capture 检测 WebSocket 帧内容：抽样 20 帧，超过 50% 静音 → **降级到文件轮询**
  10. `_poll_audio_file()` 以实时速率（20ms/帧）读取 B-leg record_session WAV，320 字节分帧送入 ASR
  11. ASR 消费音频：连续 5 帧语音（100ms）才触发 `_started_speaking`，防止开头噪音误触发
  12. 百炼 ASR 识别（8k→16k 上采样），返回中间/最终结果
  13. CallAgent._say_opening() → _say() → TTS 生成 → `uuid_displace` 写入 sofia A-leg
  14. 后续 TTS 同样通过 `uuid_displace` + `execute playback` 播放到通道
- **关键**：loopback 不是 sofia 端点，bridge 后 RTP 不会旁路
- **关键**：`uuid_audio_fork` 使用 `mono 8000` 格式（FreeSWITCH help 确认）
- **关键**：`uuid_displace` 正确目标查找（`esl_service.py` `_discover_aleg_uuid()` + `play()`）：
  - 优先级：`other_loopback_from_uuid` > `export_origination_uuid` > `origination_uuid` > `signal_bond` > `other_loopback_leg_uuid`
  - `other_loopback_from_uuid` = sofia A-leg UUID ✓（正确目标）
  - `other_loopback_leg_uuid` = loopback-a UUID ✗（写到它用户听不到）
- **首次播放延迟**：`play()` 中首次 `uuid_displace` 等待 3 秒（socket 接管媒体路径需要时间），后续播放等待 2 秒
- **ASR 兜底**：如果 ASR 未产出 `is_final=True` 结果，使用最后一条中间结果作为最终文本
- **连续语音帧检测**：需连续 5 帧（100ms）语音才认为用户开始说话，防止开头噪音误触发
- **百炼 ASR 上采样**：8kHz → 16kHz 线性插值（`paraformer-realtime-v1` 要求 16kHz 输入）
- **代码位置**：
  - `backend/services/esl_service.py` — `ESLPool.originate()`, `ESLSocketCallSession.start_audio_capture()`, `_discover_aleg_uuid()`, `_poll_audio_file()`
  - `backend/services/asr_service.py` — `BailianASRClient._upsample_8k_to_16k()`, `recognize_stream()`
  - `backend/core/call_agent.py` — `AudioStreamAdapter.stream()`, `_consecutive_speech_frames`

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

### ⚠️ `proxy_media=true` 能解决 bridge 的 RTP 旁路，但不能解决 mod_audio_fork 的静音问题
- **问题**：sofia A-leg 的 RTP 默认旁路（P2P），导致 bridge 后 RTP 不经过 FreeSWITCH 软件层
- **表现**：bridge 正常工作，但 `uuid_audio_fork` 只收到静音（max_rms=2）
- **设置**：`originate [{vars},proxy_media=true] user/1002@domain &bridge(loopback/AI_CALL)` + dialplan 中 `proxy_media=true`
- **验证**：`uuid_dump` 确认 `variable_proxy_media: true`，bridge 正常，TTS 正常播放
- **但**：mod_audio_fork media bug 仍然捕获不到音频 — 这是 mod_audio_fork 在当前 FreeSWITCH 版本中的限制
- **结论**：`proxy_media=true` 仍然需要设置（确保 bridge 正常），但不能依赖它解决 audio_fork 静音
- **日期**：2026-04-15

### ✅ `uuid_displace` 必须写到 sofia A-leg UUID
- **问题**：loopback bridge 后有两个相关 UUID：
  - `other_loopback_leg_uuid` = loopback-a（FreeSWITCH 内部端点，写到它用户听不到）
  - `other_loopback_from_uuid` = sofia A-leg（用户电话侧，写到它用户才能听到）
- **验证**：`uuid_displace(loopback-a)` 返回 +OK 但用户听不到；`uuid_displace(sofia A-leg)` 用户能听到
- **日期**：2026-04-14

### ✅ `uuid_audio_fork` 命令格式
- **格式**：`uuid_audio_fork {uuid} start {ws_url} mono 8000`
- **参数**：`mono`/`stereo`/`mixed` + `8000`/`16000`（FreeSWITCH help 确认）
- **日期**：2026-04-15

### ✅ ASR 兜底策略（已优化，部分场景不再需要兜底）
- **场景**：百炼 ASR 未产出 `is_final=True` 结果但产生多条中间结果
- **修复**：`_listen_user` 中累积 `last_intermediate_text`，检测到人声且中间结果非空时兜底使用
- **最新状态**：连续语音帧修复后，部分场景 ASR 能正常返回 `is_final=True`（如 "在哪里啊？"），不再依赖兜底
- **日期**：2026-04-15

### ✅ 百炼 ASR 完整修复（2026-04-15）
百炼 ASR 从 `NO_VALID_AUDIO_ERROR` 到成功识别，共 4 项修复：

#### 修复 1：8k→16k 上采样
- **问题**：百炼 ASR 要求 16kHz 输入（`paraformer-realtime-v1`），系统音频为 8kHz PCMU
- **修复**：`BailianASRClient` 新增 `SEND_SAMPLE_RATE = 16000` + `_upsample_8k_to_16k()` 静态方法
- **细节**：线性插值上采样，8kHz 每两个样本间插入平均值 → 16kHz
- **代码位置**：`backend/services/asr_service.py` — `BailianASRClient` 类

#### 修复 2：连续语音帧检测（防止噪音误触发）
- **问题**：文件轮询开头的几个噪音 chunk（RMS 略高于 120）误触发 `_started_speaking`，随后 300ms 静音就退出，还没读到用户说话就结束
- **修复**：`AudioStreamAdapter` 新增 `_consecutive_speech_frames` 计数器，需连续 5 帧（100ms）语音才认为用户真正开始说话
- **代码位置**：`backend/core/call_agent.py` — `AudioStreamAdapter.__init__()` + `stream()`

#### 修复 3：ASR 超时增大
- **原值**：`ASR_TIMEOUT = 12.0`
- **新值**：`ASR_TIMEOUT = 30.0`
- **原因**：文件轮询模式下录音开头有 TTS 准备期静音，用户可能在 15-20 秒后才说话，12s 太短

#### 修复 4：VAD 静音超时增大
- **原值**：`vad_silence_ms = 300`（config 默认值）
- **新值**：`vad_silence_ms = 1000`（config 默认值），运行时环境变量 `VAD_SILENCE_MS=500`
- **原因**：300ms 太短，用户说话中间短暂停顿就会误截断

#### 验证结果
- ASR 成功识别："在哪里啊？"（`is_final=True`, conf=1.00）
- 语音检测：`speech_chunks=36, max_rms=6079`
- LLM 正确响应并播放 TTS ✓
- 完整对话流程验证通过

### ✅ 文件轮询实时速率控制
- **问题**：文件轮询以全速读取并广播音频帧，ASR 服务端期望实时速率输入
- **修复**：`_poll_audio_file()` 中每帧（320 bytes）间隔 20ms 发送，模拟实时速率
- **日期**：2026-04-15

### ✅ VAD 阈值调整
- **原值**：`energy_threshold=250`
- **新值**：`energy_threshold=120`
- **原因**：文件轮询的音频能量较低（max_rms≈134），原阈值过高导致语音漏检
- **代码位置**：`backend/core/call_agent.py` — `AudioStreamAdapter.__init__()`

### ✅ TTS WAV 文件采样率
- **问题**：TTS WAV 文件以 16000Hz 写入，但实际音频为 8000Hz，导致播放速度 2 倍
- **修复**：`write_wav()` 使用 `sample_rate=8000`
- **注意**：Bailian CosyVoice TTS 使用 `AudioFormat.WAV_8000HZ_MONO_16BIT` 格式

## 项目结构
- `backend/` - Python FastAPI 后端
  - `api/` - REST API 路由
  - `core/` - 核心业务逻辑（CallAgent, TaskScheduler, 状态机）
  - `services/` - 外部服务（ESL, ASR, TTS, LLM）
- `freeswitch/conf/` - FreeSWITCH 配置（dialplan, autoload_configs, sip_profiles）
- `docker/` - Docker Compose 部署配置
- `frontend/` - 管理界面前端
