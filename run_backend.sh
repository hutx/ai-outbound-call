#!/bin/bash

# 启动AI外呼系统后端服务并输出日志
echo "Starting AI Outbound Call System Backend..."

# 从 .env 文件加载环境变量
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# 激活虚拟环境
VENV_DIR="$(cd "$(dirname "$0")" && pwd)/.venv"
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
else
    echo "Warning: Virtual environment not found at $VENV_DIR"
fi

# 设置 PYTHONPATH
export PYTHONPATH="${PYTHONPATH}:$(pwd)"

# 启动后端服务并将日志输出到控制台和日志文件
echo "Logging to: $(pwd)/logs/backend_runtime.log"
uvicorn backend.api.main:app --host 0.0.0.0 --port 8001 --reload --log-level info 2>&1 | tee -a logs/backend_runtime.log
