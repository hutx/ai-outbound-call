# 智能外呼系统 — 生产部署指南

基于 **FreeSWITCH + Claude LLM + ASR + TTS** 的完整生产级智能外呼平台。

---

## 架构总览

```
REST API / Web Console
        │
        ▼
   FastAPI 后端  ──── ESL Inbound (8021) ────► FreeSWITCH
        │                                           │
   TaskScheduler                              接通被叫
        │                                           │
   ESL Pool ──── originate ───────────────────► 通话接通
                                                    │
                                            ESL Outbound (9999)
                                                    │
                                              CallAgent
                                           ┌────────────────┐
                                           │ ASR → LLM → TTS│
                                           └────────────────┘
                                                    │
                                            挂断 / 转人工 / CDR
```

## 技术栈

| 层次 | 组件 | 说明 |
|------|------|------|
| 媒体 | FreeSWITCH 1.10 | SIP 协议、媒体流、呼叫控制 |
| ESL  | 纯 asyncio 实现 | 无需 python-ESL C 扩展 |
| ASR  | 阿里云 NLS / FunASR / 讯飞 | 实时流式识别，VAD 切句 |
| LLM  | Anthropic Claude | 意图理解、话术生成、工具调用 |
| TTS  | Edge-TTS / 阿里云 / CosyVoice | 语音合成，WAV 输出 |
| API  | FastAPI + uvicorn | REST + WebSocket 监控 |
| DB   | PostgreSQL 16 | CDR、黑名单、回拨计划 |
| 缓存 | Redis 7 | 任务状态、限频 |
| 代理 | Nginx | 限速、安全头、gzip |

---

## 快速启动

### 1. 环境准备

```bash
git clone <repo> && cd ai-outbound-call
cp .env.example .env
# 编辑 .env，至少填写：
#   ANTHROPIC_API_KEY=sk-ant-xxx
#   POSTGRES_PASSWORD=强密码
#   API_TOKEN=强随机字符串
```

### 2. 使用 uv 管理依赖（推荐）

```bash
# 安装 uv（如果尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安装项目依赖
uv sync --dev

# 启动开发服务器
./dev_start.sh

# 或者直接运行
uv run uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Docker 一键部署

```bash
# 创建日志目录
mkdir -p logs/freeswitch logs/backend

# 启动全部服务
docker-compose -f docker/docker-compose.yml up -d

# 查看日志
docker-compose -f docker/docker-compose.yml logs -f backend

# 访问管理控制台
open http://localhost
```

### 4. 本地开发（传统 pip 方式）

```bash
# 安装依赖
pip install -r requirements.txt

# 设置环境变量
export ANTHROPIC_API_KEY=sk-ant-xxx
export ASR_PROVIDER=mock
export TTS_PROVIDER=mock

# 运行 Demo（单场景）
python demo_runner.py

# 运行 Demo（四场景对比）
python demo_runner.py --multi

# 启动后端 API（需要 PostgreSQL）
cd backend && uvicorn api.main:app --reload --port 8000
```

---

## 配置说明

### 核心配置（`.env`）

```dotenv
# LLM（必填）
ANTHROPIC_API_KEY=sk-ant-api03-xxx
LLM_MODEL=claude-sonnet-4-20250514
LLM_TEMPERATURE=0.4

# FreeSWITCH
FS_ESL_PASSWORD=ClueCon          # 与 event_socket.conf.xml 一致
FS_GATEWAY=carrier_trunk          # 运营商网关名称

# ASR（三选一）
ASR_PROVIDER=ali                  # ali | funasr_local | xunfei | mock
ALI_ASR_APPKEY=your_appkey
ALI_ACCESS_KEY_ID=LTAI5txxx
ALI_ACCESS_KEY_SECRET=your_secret

# TTS
TTS_PROVIDER=edge                 # edge（免费）| ali | cosyvoice_local | mock

# 数据库
DATABASE_URL=postgresql://postgres:password@localhost:5432/outbound_call

# 安全
API_TOKEN=your_random_secret_here # 管理 API 的 Bearer Token

# 合规
MAX_CALL_DURATION=300             # 单路最长通话时长（秒）
```

### 话术脚本热加载

```bash
# 创建自定义话术文件
cat > scripts.json << 'EOF'
{
  "my_script": {
    "product_name": "产品名称",
    "product_desc": "产品简介",
    "target_customer": "目标客群",
    "key_selling_points": ["卖点1", "卖点2"],
    "objection_handling": {
      "太贵了": "我们目前有优惠活动..."
    }
  }
}
EOF

# 在 .env 中指定
SCRIPTS_PATH=/path/to/scripts.json

# 重启后端即可生效
docker restart outbound_backend
```

---

## API 参考

所有 API 需要携带 Bearer Token：
```bash
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8000/api/stats
```

### 核心端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/tasks` | 创建并启动外呼任务 |
| `GET`  | `/api/tasks` | 任务列表 |
| `POST` | `/api/tasks/{id}/pause` | 暂停任务 |
| `POST` | `/api/tasks/{id}/resume` | 恢复任务 |
| `DELETE` | `/api/tasks/{id}` | 取消任务 |
| `GET`  | `/api/calls` | 通话记录（支持过滤） |
| `GET`  | `/api/calls/stats` | 聚合统计 |
| `GET`  | `/api/blacklist` | 黑名单列表 |
| `POST` | `/api/blacklist` | 添加黑名单 |
| `DELETE` | `/api/blacklist/{phone}` | 移除黑名单 |
| `GET`  | `/api/scripts` | 话术模板列表 |
| `GET`  | `/api/callbacks` | 待回拨名单 |
| `GET`  | `/api/stats` | 实时统计（无需鉴权） |
| `GET`  | `/health` | 健康检查 |
| `GET`  | `/metrics` | Prometheus 指标（仅内网） |

### 创建任务示例

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Q2 理财推广",
    "phone_numbers": ["13800138001", "13800138002"],
    "script_id": "finance_product_a",
    "concurrent_limit": 10,
    "max_retries": 1,
    "caller_id": "202603311547"
  }'
```

### WebSocket 监控

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/monitor?token=your_token');
ws.onmessage = (e) => {
  const data = JSON.parse(e.data);
  // { type: "stats", active_calls: 5, active_tasks: 2, tasks: [...] }
};
```

---

## 运营商接入

编辑 `freeswitch/conf/vars.xml`：

```xml
<X-PRE-PROCESS cmd="set" data="carrier_sip_server=sip.your-carrier.com"/>
<X-PRE-PROCESS cmd="set" data="carrier_username=YOUR_SIP_USER"/>
<X-PRE-PROCESS cmd="set" data="carrier_password=YOUR_SIP_PASSWORD"/>
<X-PRE-PROCESS cmd="set" data="outbound_caller_id=202603311547"/>
```

编辑 `freeswitch/conf/autoload_configs/sofia.conf.xml`，在 `carrier_trunk` gateway 中填写实际信息。

---

## 生产运维

### 监控

```bash
# Prometheus 接入
# 在 prometheus.yml 中添加：
scrape_configs:
  - job_name: 'outbound-call'
    static_configs:
      - targets: ['your-server-ip:8000']
    metrics_path: /metrics
    # 需配置 basic_auth 或通过内网访问

# 关键指标
# outbound_calls_total        - 累计外呼量
# outbound_calls_active       - 当前并发路数
# outbound_calls_completed_total  - 完成量
# outbound_tasks_active       - 活跃任务数
```

### 日志

```bash
# 后端日志
docker logs outbound_backend -f

# FreeSWITCH 控制台
docker exec -it outbound_freeswitch fs_cli

# FreeSWITCH 日志
docker exec outbound_freeswitch tail -f /var/log/freeswitch/freeswitch.log

# Nginx 访问日志
docker logs outbound_nginx -f
```

### 数据库

```bash
# 备份
pg_dump -h localhost -U postgres outbound_call > backup_$(date +%Y%m%d).sql

# 查看 KPI 视图
psql -h localhost -U postgres outbound_call -c "SELECT * FROM vw_today_stats;"
psql -h localhost -U postgres outbound_call -c "SELECT * FROM vw_task_stats ORDER BY last_call_at DESC LIMIT 10;"
```

### 扩容

并发上限由以下因素共同决定：
- `MAX_CONCURRENT_CALLS` 环境变量
- FreeSWITCH 服务器 CPU / 内存（4核8G 建议 ≤50 路，8核16G ≤100 路）
- ASR/TTS API 的 QPS 限制
- 运营商 SIP 中继并发授权数

---

## 合规要求

1. **开场白合规**：系统自动在开场白中播报"本通话由人工智能完成"
2. **拨号时间窗口**：默认 09:00–21:00，在 `scheduler.py` 中可调整
3. **黑名单机制**：用户拒绝 2 次自动加入黑名单，或通话中检测到"不要再打"
4. **通话录音**：默认保存到 `/recordings/{task_id}/{uuid}.wav`，保留 ≥ 6 个月
5. **号码备案**：外显主叫号码需在工信部完成备案
6. **单日限频**：建议同一号码每日外呼不超过 3 次（在 `scheduler.py` 中配置重试间隔）

---

## 目录结构

```
ai-outbound-call/
├── backend/
│   ├── api/main.py              # FastAPI 入口，全部 REST API
│   ├── core/
│   │   ├── call_agent.py        # 通话主循环（ASR→LLM→TTS）
│   │   ├── config.py            # 环境变量配置
│   │   ├── scheduler.py         # 外呼任务调度（并发/重试/时间窗口）
│   │   └── state_machine.py     # 通话状态机 + 意图路由
│   ├── services/
│   │   ├── esl_service.py       # FreeSWITCH ESL（连接池 + Session）
│   │   ├── asr_service.py       # ASR（阿里云/FunASR/讯飞/Mock）
│   │   ├── tts_service.py       # TTS（Edge/阿里云/CosyVoice/Mock）
│   │   ├── llm_service.py       # Claude API（重试/话术热加载）
│   │   └── crm_service.py       # CRM（黑名单/意向/回拨/短信）
│   └── utils/
│       ├── db.py                # PostgreSQL ORM（CDR/黑名单/回拨）
│       ├── audio.py             # 音频工具（VAD/G.711/WAV）
│       └── tts_cache.py         # TTS 缓存定期清理
├── freeswitch/conf/             # FreeSWITCH 配置文件
├── frontend/index.html          # 管理控制台（单文件，无依赖）
├── docker/
│   ├── docker-compose.yml
│   ├── Dockerfile.backend       # 多阶段构建
│   ├── init.sql                 # 数据库初始化 + 视图
│   └── nginx.conf               # 限速/gzip/安全头
├── tests/test_all.py            # 单元测试（44 项）
├── demo_runner.py               # 本地 Demo（无需 FreeSWITCH）
├── freeswitch_check.sh          # FreeSWITCH 启动前检查
├── requirements.txt
└── .env.example
```
