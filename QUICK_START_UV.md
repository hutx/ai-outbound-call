# 快速启动指南 (uv 版本)

本指南介绍如何使用 uv（现代 Python 包管理器）快速启动 AI 外呼系统。

## 前提条件

- Python 3.12 或更高版本 (推荐 Python 3.13+ 以支持所有功能)
- uv 包管理器
- PostgreSQL 数据库 (用于生产) 或可用的 .env 配置

## 安装 uv

```bash
# 使用官方安装脚本
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 克隆并设置项目

```bash
# 克隆项目
git clone <your-repo> && cd ai-outbound-call

# 复制环境变量文件
cp .env.example .env
# 编辑 .env 文件，至少设置以下变量：
# - ANTHROPIC_API_KEY=sk-ant-xxx
# - POSTGRES_PASSWORD=your_password
# - API_TOKEN=your_api_token
```

## 使用 uv 安装依赖

```bash
# 安装所有依赖（包括开发依赖）
uv sync --dev

# 或只安装生产依赖
uv sync --locked --no-dev
```

## 启动服务

### 开发模式

```bash
# 方法 1: 使用开发启动脚本
./dev_start.sh

# 方法 2: 直接使用 uv run
uv run uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug
```

### 生产模式

```bash
# 使用生产启动脚本
./prod_start.sh
```

### 运行 Demo（无需 FreeSWITCH）

```bash
# 单场景 Demo
uv run python demo_runner.py

# 多场景对比 Demo
uv run python demo_runner.py --multi
```

## Docker 部署

### 使用 uv 优化的 Dockerfile

```bash
# 构建使用 Python 3.13 和 uv 的镜像
docker build -f docker/Dockerfile.backend.uv -t ai-outbound-call:uv .

# 运行容器
docker run -d \
  --name outbound_backend \
  -p 8000:8000 -p 9999:9999 \
  -v $(pwd)/logs:/var/log/outbound \
  -v $(pwd)/recordings:/recordings \
  --env-file .env \
  ai-outbound-call:uv
```

## 环境验证

运行环境检查脚本：

```bash
./check_uv_env.sh
```

## 常见问题

### Q: 如果我使用 Python < 3.13 且遇到 audioop-lts 错误怎么办？

A: `audioop-lts` 包需要 Python 3.13+。如果您的环境中没有这个包的需求，可以从 `pyproject.toml` 中移除它。

### Q: uv sync 失败怎么办？

A:
1. 检查 Python 版本: `python3 --version`
2. 确保 uv 最新: `uv self update`
3. 清理缓存: `uv cache clean`

### Q: 如何更新依赖？

A:
- 更新单个包: `uv add package_name --upgrade`
- 更新所有包: `uv sync --refresh`
- 更新锁文件: `uv lock --upgrade`

## 验证安装

确认服务正常启动：

```bash
curl http://localhost:8000/health
```

应该返回类似这样的响应：
```json
{
  "status": "ok",
  "esl_pool": false,
  "active_calls": 0,
  "timestamp": "2024-01-01T00:00:00"
}
```

## 下一步

- 查看 API 文档: `http://localhost:8000/docs` (开发模式)
- 管理界面: `http://localhost:8000`
- 创建外呼任务: 使用 `/api/tasks` 端点