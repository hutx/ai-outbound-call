#!/bin/bash
# uv 环境检查脚本

echo "🔍 检查 uv 环境..."

# 检查 uv 是否已安装
if ! command -v uv &> /dev/null; then
    echo "❌ uv 未安装"
    echo "安装 uv:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "✅ uv 已安装: $(uv --version)"

# 检查 pyproject.toml 是否存在
if [ ! -f "pyproject.toml" ]; then
    echo "❌ pyproject.toml 不存在"
    exit 1
fi

echo "✅ pyproject.toml 存在"

# 检查是否需要同步依赖
if [ ! -f "uv.lock" ] || [ "pyproject.toml" -nt "uv.lock" ]; then
    echo "🔄 依赖可能已过期，建议运行: uv sync"
else
    echo "✅ 依赖已是最新"
fi

# 检查虚拟环境
if uv venv --exists 2>/dev/null; then
    echo "✅ 虚拟环境存在"
else
    echo "⚠️  虚拟环境不存在，将创建: uv venv"
fi

echo "🎉 uv 环境检查完成"