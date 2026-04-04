# AI 外呼系统启动指南

本指南介绍了启动 AI 外呼系统的几种方式。

## 启动方式概览

1. **本地开发模式** - 使用 uv 管理依赖，最快启动方式
2. **Docker 简化模式** - 仅启动 API 和数据库服务
3. **Docker 完整模式** - 启动包括 FreeSWITCH 在内的完整系统（需要解决镜像问题）

## 1. 本地开发模式（推荐）

这种方式最快，直接使用 uv 运行。

### 环境准备
- 确保安装了 Python 3.12+
- 确保安装了 uv: `pip install uv`

### 启动步骤

```bash
# 1. 安装依赖
uv sync --dev

# 2. 启动 API 服务
uv run uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 --reload

# 3. 或运行 Demo（无需 FreeSWITCH）
uv run python demo_runner.py
```

## 2. Docker 简化模式

这种方式使用基础 Python 镜像，安装依赖后运行，避免复杂的构建过程。

### 启动步骤

```bash
# 确保 .env 文件已配置

# 启动服务
docker-compose -f docker/docker-compose.dev.yml up -d

# 查看日志
docker-compose -f docker/docker-compose.dev.yml logs -f backend

# 停止服务
docker-compose -f docker/docker-compose.dev.yml down
```

### 验证服务
服务启动后，访问：
- API 服务: http://localhost:8000
- API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health

## 3. Docker 完整模式（高级）

**注意**: 目前 FreeSWITCH 镜像存在问题，需要修复。

如果需要完整部署，包括 FreeSWITCH，请使用原始的 docker-compose.yml，但需要解决以下问题：

- 替换可用的 FreeSWITCH 镜像
- 修复 FunASR 镜像问题（或使用 mock 模式）

## 配置说明

### 环境变量
- `ANTHROPIC_API_KEY`: Anthropic API 密钥（必须）
- `POSTGRES_PASSWORD`: 数据库密码
- `REDIS_PASSWORD`: Redis 密码
- `ASR_PROVIDER`: ASR 提供商 (mock | ali | xunfei | funasr_local)
- `TTS_PROVIDER`: TTS 提供商 (mock | edge | ali | cosyvoice_local)

### Mock 模式
对于开发测试，建议使用 mock 模式：
```bash
ASR_PROVIDER=mock
TTS_PROVIDER=mock
```

## 故障排除

### 依赖安装问题
- 如果 uv 安装失败，可以使用传统方式: `pip install -r requirements.txt`

### 端口占用
- 默认 API 端口: 8000
- 默认数据库端口: 5432
- 默认 Redis 端口: 6379

### 镜像拉取失败
- 检查网络连接
- 确认镜像仓库地址
- 使用国内镜像源

## 开发与测试

### 运行 Demo
```bash
# 单场景 Demo
uv run python demo_runner.py

# 多场景 Demo
uv run python demo_runner.py --multi
```

### API 访问
使用 Postman 或 curl 测试 API：
```bash
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8000/api/stats
```

## 生产部署

生产环境需要：
- 正式的 FreeSWITCH 配置
- 有效的 ASR/TTS 服务商配置
- SSL 证书配置
- 监控和日志收集配置