# LiveKit 智能外呼系统

基于 **LiveKit Agents + SIP + Kamailio** 的完整生产级智能外呼平台。

## 架构总览

```
  ┌─────────────────────────────────────────────────────────────┐
  │                      Docker Compose                         │
  │                                                             │
  │  REST API ◄──► FastAPI ──────────┐                          │
  │                                  │                          │
  │  LiveKit Server ◄────────────────┼── ws://:7880            │
  │       │                          │                          │
  │       ├─ LiveKit SIP ◄─── SIP ───┼── 5060 (PSTN 网关)      │
  │       │                          │                          │
  │       └─ Kamailio ◄───── SIP ────┼── 5080 (软电话注册)      │
  │                                  │                          │
  │  Agent Worker ◄──────────────────┘  (LiveKit AgentSession)  │
  │       │                           STT → LLM → TTS          │
  │       │                                                      │
  │  PostgreSQL ◄─── CDR / 话术 / 任务 / 录音详情               │
  │  Redis        ◄─── LiveKit 内部消息总线                      │
  │  MinIO        ◄─── 录音文件存储 (S3 兼容)                    │
  │  LiveKit Egress ◄── 通话录制 → 上传 MinIO                    │
  └─────────────────────────────────────────────────────────────┘
```

### 外呼流程

1. **API 创建外呼任务** → FastAPI 调用 LiveKit SIP API 发起 SIP INVITE
2. **Kamailio 路由** → 添加号码前缀（如 `97776`）后转发到 SIP 运营商网关
3. **用户接听** → LiveKit Server 创建 Room，Agent Worker 加入
4. **AI 对话** → AgentSession（STT → LLM → TTS）开始多轮对话
5. **录音** → LiveKit Egress 录制通话音频 → 上传 MinIO
6. **CDR 归档** → 通话结束 → 写入通话记录到 PostgreSQL

## 技术栈

| 层次 | 组件 | 说明 |
|------|------|------|
| SIP 协议 | LiveKit SIP | SIP 网关，对接 PSTN 运营商 |
| SIP 代理 | Kamailio 5.4 | 软电话注册、号码路由、前缀处理 |
| 媒体服务 | LiveKit Server v1.7 | 实时音视频服务器、Room 管理 |
| AI Agent | LiveKit Agents 0.12+ | AgentSession 框架，串联 STT/LLM/TTS |
| STT | 阿里云 Paraformer / 通义千问 ASR | 流式语音识别 |
| LLM | 通义千问 Qwen (DashScope) | 意图理解、对话生成 |
| TTS | 阿里云 CosyVoice | 语音合成 |
| API | FastAPI + uvicorn | REST API |
| DB | PostgreSQL 16 | CDR、话术、任务、录音详情 |
| 缓存 | Redis 7 | LiveKit 内部消息总线 |
| 存储 | MinIO | S3 兼容对象存储（录音文件） |
| 录制 | LiveKit Egress v1.8 | 通话录制 → 上传 MinIO |

## 项目结构

```
ai-outbound-call-new/
├── backend/                          # Python 后端
│   ├── agent/
│   │   ├── outbound_agent.py         # 外呼 Agent 主逻辑（CDR、对话循环、录音归档）
│   │   ├── dialog_manager.py         # 对话管理器
│   │   └── worker.py                 # Agent Worker 入口
│   ├── api/
│   │   ├── main.py                   # FastAPI 主入口
│   │   ├── calls_api.py              # 通话记录 API
│   │   ├── tasks_api.py              # 外呼任务 API
│   │   ├── scripts_api.py            # 话术脚本 API
│   │   └── monitor_api.py            # 监控 API
│   ├── core/
│   │   ├── config.py                 # 配置单例（pydantic-settings）
│   │   └── events.py                 # 事件模型（CallState/Intent/Result）
│   ├── models/
│   │   ├── call_record.py            # 通话记录模型
│   │   ├── call_record_detail.py     # 通话详情模型（问答记录）
│   │   ├── task.py                   # 外呼任务模型
│   │   ├── script.py                 # 话术脚本模型
│   │   └── file.py                   # 文件记录模型
│   ├── plugins/
│   │   ├── aliyun_stt.py             # 阿里云 STT 插件
│   │   ├── aliyun_tts.py             # 阿里云 TTS 插件
│   │   └── qwen_llm.py               # 通义千问 LLM 插件
│   ├── services/
│   │   ├── sip_service.py            # SIP 外呼服务
│   │   ├── egress_service.py         # Egress 录制服务
│   │   ├── minio_service.py          # MinIO 对象存储
│   │   ├── call_record_service.py    # 通话记录 CRUD
│   │   ├── call_record_detail_service.py  # 通话详情 CRUD
│   │   ├── file_service.py           # 文件记录服务
│   │   ├── task_service.py           # 任务管理 + SIP 服务封装
│   │   └── script_service.py         # 话术脚本管理
│   ├── scripts/
│   │   ├── init_sip_trunk.py         # SIP Trunk 初始化脚本
│   │   └── migrate_add_egress_tables.py  # 数据库迁移
│   ├── utils/
│   │   └── db.py                     # PostgreSQL 异步连接池
│   ├── frontend/                     # 前端静态文件（可选）
│   └── pyproject.toml                # 项目依赖
├── Dockerfile.backend                # 后端 Dockerfile
├── docker-compose.yml                # Docker Compose 编排
├── docker/
│   ├── livekit.yaml                  # LiveKit Server 配置
│   ├── sip-config.yaml               # LiveKit SIP 配置
│   ├── kamailio/
│   │   ├── kamailio.cfg              # Kamailio SIP 路由配置
│   │   └── init_kamailio_db.sh       # Kamailio 数据库初始化脚本
│   └── minio_data/                   # MinIO 数据目录
├── docs/                             # 历史文档（FreeSWITCH 架构迁移资料）
├── frontend/                         # 管理界面前端
├── init.sql                          # PostgreSQL 初始化脚本
├── .env.example                      # 环境变量模板
└── README.md                         # 本文件
```

## 快速启动

### 1. 环境准备

```bash
git clone <repo> && cd ai-outbound-call-new
cp .env.example .env
# 编辑 .env，至少填写：
#   LK_LIVEKIT_API_KEY / LK_LIVEKIT_API_SECRET
#   LK_ALIYUN_ASR_API_KEY / LK_ALIYUN_TTS_API_KEY / LK_ALIYUN_LLM_API_KEY
#   LK_PG_PASSWORD
#   LK_SIP_PROVIDER_HOST / LK_SIP_PROVIDER_PORT
```

### 2. Docker Compose 一键部署

```bash
# 启动全部服务
docker compose up -d

# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f api
docker compose logs -f agent
docker compose logs -f kamailio
```

### 3. 本地开发

```bash
cd backend

# 使用 uv（推荐）
uv sync
uv run python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# 或使用 pip
pip install -r requirements.txt
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. 访问服务

| 服务 | 地址 | 说明 |
|------|------|------|
| FastAPI 文档 | http://localhost:8000/docs | Swagger UI |
| API 根路径 | http://localhost:8000 | API 信息 |
| LiveKit Server | http://localhost:7880 | LiveKit 管理 |
| Kamailio SIP | localhost:5080 | 软电话注册 |
| MinIO 控制台 | http://localhost:9001 | 录音文件管理 |

## 配置说明

### 环境变量（`.env`）

至少填写以下配置：

```dotenv
# LiveKit
LK_LIVEKIT_API_KEY=devkey
LK_LIVEKIT_API_SECRET=secret

# AI 服务（阿里云百炼）
LK_ALIYUN_ASR_API_KEY=your_asr_key
LK_ALIYUN_TTS_API_KEY=your_tts_key
LK_ALIYUN_LLM_API_KEY=your_llm_key
LK_ALIYUN_LLM_MODEL=qwen3.5-plus

# PostgreSQL
LK_PG_PASSWORD=strong_password

# SIP 运营商网关
LK_SIP_PROVIDER_HOST=your_sip_provider_ip
LK_SIP_PROVIDER_PORT=your_sip_provider_port
LK_SIP_PROVIDER_PREFIX=97776
LK_SIP_PROVIDER_CALLER_ID=your_caller_id
```

完整配置项见 `.env.example`。

### 话术脚本

话术脚本通过 API 管理（`/api/scripts`），支持：
- 开场白 / 结束语配置
- 打断策略（保护期、宽容期）
- 无响应处理（超时、追问、自动挂断）

默认话术在 `init.sql` 中初始化。

## API 参考

### 端点列表

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/scripts` | 创建话术脚本 |
| `GET`  | `/api/scripts` | 话术列表 |
| `GET`  | `/api/scripts/{id}` | 话术详情 |
| `PUT`  | `/api/scripts/{id}` | 更新话术 |
| `DELETE` | `/api/scripts/{id}` | 删除话术 |
| `POST` | `/api/tasks` | 创建外呼任务 |
| `GET`  | `/api/tasks` | 任务列表 |
| `GET`  | `/api/tasks/{id}` | 任务详情 |
| `PUT`  | `/api/tasks/{id}` | 更新任务 |
| `DELETE` | `/api/tasks/{id}` | 删除任务 |
| `GET`  | `/api/calls` | 通话记录列表 |
| `GET`  | `/api/calls/{id}` | 通话详情 |
| `GET`  | `/api/monitor/events` | 实时事件流（SSE） |
| `GET`  | `/health` | 健康检查 |

### 创建外呼任务示例

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "产品推广外呼",
    "phones": ["13800138001", "13800138002"],
    "script_id": "default",
    "concurrent_limit": 5
  }'
```

## 数据库

### 核心表

| 表名 | 说明 |
|------|------|
| `lk_scripts` | 话术脚本（开场白、主提示、打断策略等） |
| `lk_tasks` | 外呼任务（号码列表、状态、统计） |
| `lk_task_phones` | 任务号码（每个号码的呼叫状态） |
| `lk_call_records` | 通话记录 CDR（状态、意图、时长、录音） |
| `lk_call_record_details` | 通话详情（每轮问答记录、延迟指标） |
| `lk_files` | 文件记录（录音文件元信息） |

### 初始化

`init.sql` 会自动创建所有表和索引，首次启动时执行：

```bash
# 在 PostgreSQL 中执行
psql -h localhost -U postgres -d ai_outbound_livekit -f init.sql
```

或使用 Docker Compose 的 `postgres` 服务自动初始化。

## 生产运维

### 日志

```bash
# API 日志
docker compose logs -f api

# Agent 日志
docker compose logs -f agent

# LiveKit Server 日志
docker compose logs -f livekit-server

# Kamailio 日志
docker compose logs -f kamailio

# MinIO 日志
docker compose logs -f minio
```

### 数据库操作

```bash
# 连接数据库
docker compose exec postgres psql -U postgres -d ai_outbound_livekit

# 查看通话记录
docker compose exec postgres psql -U postgres -d ai_outbound_livekit \
  -c "SELECT call_id, phone, status, duration_sec FROM lk_call_records ORDER BY created_at DESC LIMIT 10;"

# 查看任务统计
docker compose exec postgres psql -U postgres -d ai_outbound_livekit \
  -c "SELECT name, status, total_phones, completed_count FROM lk_tasks;"
```

### 扩容

并发上限由以下因素决定：
- LiveKit Server 性能（4核8G 建议 ≤50 路）
- STT/TTS/LLM API 的 QPS 限制
- SIP 运营商中继并发授权数

## 运营商接入

### SIP Trunk 配置

在 `.env` 中配置运营商网关：

```dotenv
LK_SIP_PROVIDER_HOST=your_sip_provider_ip
LK_SIP_PROVIDER_PORT=your_sip_provider_port
LK_SIP_PROVIDER_PREFIX=your_prefix   # 拨号前缀（可选）
LK_SIP_PROVIDER_CALLER_ID=your_id    # 外显号码
```

### Kamailio 路由

Kamailio 负责：
- 软电话用户注册（端口 5080）
- PSTN 外呼路由（添加前缀后转发到运营商）
- NAT 穿透处理

配置文件：`docker/kamailio/kamailio.cfg`

## 核心特性

- **AI 智能对话**：STT → LLM → TTS 全链路，支持多轮自然对话
- **打断（Barge-in）**：用户可在 AI 播报期间随时插话，含保护期和噪声过滤
- **宽容期（Tolerance）**：AI 播报后短时内用户说话可重新触发理解
- **无响应处理**：超时追问 → 最大次数后自动挂断
- **通话录音**：LiveKit Egress 录制 → MinIO 存储
- **通话详情**：逐轮问答记录 + 延迟指标分析
- **话术管理**：REST API 管理话术脚本，支持多种场景
- **任务管理**：批量外呼、并发控制、失败重试
- **实时监控**：SSE 事件流，实时查看通话状态变化
