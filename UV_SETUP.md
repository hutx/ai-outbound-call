# 使用 uv 管理 AI 外呼系统

本文档介绍如何使用 uv（下一代 Python 包管理器）来管理 AI 外呼系统的依赖和启动服务。

## 什么是 uv？

uv 是一个超快的 Python 包管理器，用 Rust 编写，比传统的 pip 和 poetry 更快。它提供了：

- 快速的依赖解析和安装
- 兼容 pip 和 venv 的工作流
- 改进的虚拟环境管理
- 更快的同步和锁定过程

## 安装 uv

### 方法 1: 使用官方安装脚本

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 方法 2: 使用 Homebrew (macOS)

```bash
brew install uv
```

### 方法 3: 使用 pip

```bash
pip install uv
```

## 项目设置

本项目已经配置了 `pyproject.toml` 和 `uv.lock` 文件，使用 uv 管理所有依赖。

### 安装依赖

```bash
# 安装所有依赖（包括开发依赖）
uv sync --dev

# 安装生产依赖
uv sync --locked
```

### 激活虚拟环境

```bash
# uv 会自动为您管理虚拟环境
# 如果需要手动激活，可以使用：
source .venv/bin/activate
```

## 启动服务

### 开发模式

```bash
# 使用开发启动脚本
./dev_start.sh

# 或直接运行
uv run uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug
```

### 生产模式

```bash
# 使用生产启动脚本
./prod_start.sh

# 或直接运行
uv run uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 --workers 1 --log-level info
```

### Demo 模式

```bash
# 运行 Demo（无需 FreeSWITCH）
uv run python demo_runner.py

# 运行多场景 Demo
uv run python demo_runner.py --multi
```

## Docker 支持

项目包含两个 Dockerfile：

1. `docker/Dockerfile.backend` - 修改后的 Dockerfile，使用 uv
2. `docker/Dockerfile.backend.uv` - 专为 uv 优化的 Dockerfile（使用 Python 3.13）

注意：由于 `audioop-lts` 包需要 Python 3.13+，如果您使用原版 Dockerfile，可能需要移除此依赖或使用 Python 3.13。

## 依赖管理

### 更新依赖

```bash
# 更新依赖并重新锁定
uv pip compile pyproject.toml -o uv.lock

# 或者同步并更新
uv sync --refresh
```

### 添加新依赖

```bash
# 添加运行时依赖
uv add package_name

# 添加开发依赖
uv add --dev package_name
```

## 环境检查

使用提供的检查脚本验证环境：

```bash
./check_uv_env.sh
```

## 项目结构

- `pyproject.toml` - 项目配置和依赖定义
- `uv.lock` - 锁定的依赖版本
- `dev_start.sh` - 开发模式启动脚本
- `prod_start.sh` - 生产模式启动脚本
- `start_with_uv.sh` - 通用 uv 启动脚本
- `check_uv_env.sh` - 环境检查脚本

## 故障排除

### 如果遇到 audioop-lts 兼容性问题

audioop-lts 需要 Python 3.13+，如果您的环境是较低版本，请考虑：

1. 升级到 Python 3.13+
2. 或在 pyproject.toml 中移除此依赖（如果是非必需的）
3. 或使用专门的 Python 3.13 Docker 映像

### 性能提示

- uv 比 pip 快得多，特别是在首次安装时
- 使用 `uv run` 来运行项目中的命令，它会自动激活虚拟环境
- `uv sync` 比 `pip install -r requirements.txt` 快得多