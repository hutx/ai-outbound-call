# AI 外呼系统后端代码分析报告

## 项目概述

这是一个基于 FreeSWITCH + Claude LLM 的生产级智能外呼系统，实现了完整的 AI 对话流程，包括 ASR（语音识别）、LLM（对话理解）、TTS（语音合成）三大核心组件。

## 架构概览

```
┌─────────────────┐    SIP信令    ┌─────────────────┐    媒体流    ┌─────────────────┐
│   Zoiper        │ ────────────▶ │   FreeSWITCH    │ ──────────▶ │   后端服务      │
│  软电话客户端    │               │                 │             │                 │
└─────────────────┘               └─────────────────┘             └─────────────────┘
                                         │                              │
                                    ESL Outbound                ESL Inbound/Outbound
                                         │                              │
                                   ┌─────▼─────┐                  ┌─────▼─────┐
                                   │  CallAgent │                  │  API服务   │
                                   │           │◀─────────────────┤           │
                                   └───────────┘                  └───────────┘
                                        │
                                   ┌────▼────┐
                                   │ASR/LLM/  │
                                   │TTS引擎   │
                                   └─────────┘
```

## 核心模块分析

### 1. 主入口模块 (api/main.py)

**功能**: FastAPI 主入口，负责系统初始化和生命周期管理

**关键特性**:
- 使用 `lifespan` 管理系统启动/关闭流程
- 初始化所有核心服务组件（ASR、TTS、LLM、ESL、WebSocket）
- 实现优雅关闭机制，等待活跃通话完成
- 集成 Prometheus 风格的监控指标

**启动流程**:
1. 数据库初始化
2. CRM 黑名单服务预热
3. ASR/TTS/LLM 服务初始化
4. AudioStream WebSocket 服务器启动
5. ESL 连接池初始化
6. ESL Socket 服务器启动（监听 FreeSWITCH 连接）

### 2. 通话代理 (core/call_agent.py)

**功能**: 处理单路通话的完整生命周期

**核心组件**:
- `AudioStreamAdapter`: 将 FreeSWITCH 音频流转换为 ASR 可消费的格式
- `CallState`: 通话状态机（RINGING → LISTENING → SPEAKING → ENDED）
- `CallContext`: 通话上下文信息（UUID、任务ID、号码、脚本ID等）

**主要流程**:
1. 接收 FreeSWITCH 连接
2. 从 channel 变量提取任务信息
3. 启动音频流处理
4. 循环处理：ASR → LLM → TTS
5. 支持用户打断（barge-in）
6. 通话结束后更新统计信息

**生产特性**:
- 三级错误降级机制（ASR/LLM/TTS）
- 通话时长限制防挂死
- VAD（语音活动检测）优化
- 打断保护机制

### 3. ESL 服务 (services/esl_service.py)

**功能**: 与 FreeSWITCH 的 ESL（Event Socket Library）通信

**两个模式**:
- **Inbound ESL**: 后端主动连接 FreeSWITCH:8021，用于发起外呼
- **Outbound ESL**: FreeSWITCH 主动连接后端:9999，每路通话独立 session

**核心类**:
- `AsyncESLConnection`: 单条 ESL 连接管理
- `AsyncESLPool`: ESL 连接池，复用连接避免频繁新建
- `ESLSocketServer`: 接收 FreeSWITCH 主动连接的服务器
- `ESLSocketCallSession`: 每路通话的会话封装

**关键功能**:
- originate 命令发起外呼
- execute 命令控制播放/录音等
- 事件订阅和处理
- 音频流捕获

### 4. 音频流 WebSocket (services/audio_stream_ws.py)

**功能**: 接收 FreeSWITCH 通过 `mod_audio_stream` 推送的实时音频流

**工作原理**:
1. FreeSWITCH 拨号计划执行 `audio_stream` 指令
2. mod_audio_stream 连接此 WebSocket 服务器
3. 首帧包含 Channel UUID
4. 后续每 20ms 推送一帧 PCM 音频
5. 音频帧直接送入对应通话的队列

**优势**:
- 低延迟（20ms 帧）
- 高可靠性（连接断开自动清理）
- 可观测性（连接状态、帧计数、丢帧率）

### 5. ASR 服务 (services/asr_service.py)

**功能**: 语音识别服务抽象层

**支持的提供商**:
- FunASR（本地部署）
- 阿里云 NLS
- 讯飞实时转写
- 阿里云百炼
- Mock（测试用）

**流式识别**: 支持实时语音转文字，边说边识别

**关键特性**:
- 异常处理和降级
- 配置化的 VAD（静音检测）
- 识别置信度返回

### 6. TTS 服务 (services/tts_service.py)

**功能**: 文本转语音服务抽象层

**支持的提供商**:
- 阿里云 TTS
- 阿里云百炼 CosyVoice
- Edge TTS
- 本地 CosyVoice
- Mock（测试用）

**输出格式**: WAV 文件（FreeSWITCH 可直接播放）

### 7. LLM 服务 (services/llm_service.py)

**功能**: 对话理解和话术生成

**支持的提供商**:
- Anthropic Claude（主要）
- 兼容 OpenAI 格式的其他 LLM
- Mock（测试用）

**对话管理**: 维护对话历史，支持上下文理解

### 8. 任务调度器 (core/scheduler.py)

**功能**: 外呼任务管理和调度

**核心特性**:
- 并发控制（全局和任务级）
- 失败重试（指数退避）
- 黑名单过滤
- 时间窗口限制
- 任务进度跟踪

**调度策略**:
- 按任务配置的并发数限制
- 全局并发数限制
- 号码间隔控制
- 重试机制

### 9. 配置管理 (core/config.py)

**功能**: 集中管理所有配置项

**配置分类**:
- FreeSWITCH 连接配置
- ASR/TTS/LLM 服务配置
- 数据库和 Redis 配置
- 应用级配置

## 关键集成点

### 1. FreeSWITCH 与后端集成

**拨号计划集成**:
- FreeSWITCH 拨号计划触发 `socket` 应用连接后端
- 通过 channel 变量传递任务信息
- 使用 `audio_stream` 推送音频流到 WebSocket

**ESL 通信**:
- Inbound: 后端发起，用于控制 FreeSWITCH
- Outbound: FreeSWITCH 发起，用于每路通话

### 2. 语音数据流转

```
被叫声音 → FreeSWITCH → audio_stream WebSocket → ASR → LLM → TTS → FreeSWITCH → 播放给被叫
```

### 3. 通话控制流程

1. 任务调度器通过 ESL originate 发起外呼
2. FreeSWITCH 接通后主动连接后端 ESL Socket Server
3. CallAgent 接管通话处理
4. 实时音频流通过 WebSocket 传输
5. ASR/LLM/TTS 处理对话逻辑
6. 通话结束时更新统计数据

## 生产特性

### 1. 容错和降级
- 服务级别的健康检查
- 三级错误降级（ASR → LLM → TTS）
- 连接池和重连机制

### 2. 监控和可观测性
- 通话统计指标
- 实时监控 API
- 详细的日志记录
- 性能指标跟踪

### 3. 安全性
- API Token 鉴权
- 黑名单过滤
- 呼叫时间窗口控制

### 4. 可扩展性
- 模块化设计
- 配置化服务提供商
- 并发控制机制

## 总结

该 AI 外呼系统是一个设计完善的生产级解决方案，具有以下特点：

1. **架构清晰**: 模块化设计，职责分离
2. **生产就绪**: 包含容错、监控、安全等生产特性
3. **可扩展**: 支持多种 ASR/TTS/LLM 服务提供商
4. **高性能**: WebSocket 实时音频流，低延迟处理
5. **可观察**: 完整的监控和日志体系

系统成功整合了 FreeSWITCH 的媒体处理能力和现代 AI 服务，形成了一个完整的智能外呼解决方案。