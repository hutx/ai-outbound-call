#!/bin/bash

# 构建增强版 FreeSWITCH 镜像
# 包含 mod_audio_stream 模块和支持 AI 外呼功能

set -e  # 遏现错误时退出

echo "🚀 开始构建增强版 FreeSWITCH 镜像..."

# 检查 Docker 是否运行
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker 未运行，请启动 Docker 后再执行此脚本"
    exit 1
fi

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "📁 项目根目录: $PROJECT_ROOT"
echo "📦 Docker 目录: $DOCKER_DIR"

# 构建镜像
IMAGE_NAME="freeswitch-ai"
TAG="latest"

echo "🔨 构建 $IMAGE_NAME:$TAG 镜像..." 
cd $DOCKER_DIR
docker build -f Dockerfile.freeswitch-ai -t $IMAGE_NAME:$TAG --platform linux/amd64 .

if [ $? -eq 0 ]; then
    echo "✅ 镜像构建成功: $IMAGE_NAME:$TAG"
    
    # 验证镜像
    echo "🔍 验证镜像..."
    
    echo ""
    echo "🎉 构建完成！"
    echo ""
    echo "📝 接下来的操作："
    echo "1. 更新 docker-compose.yml 中的 freeswitch 服务镜像："
    echo "   image: $IMAGE_NAME:$TAG"
    echo ""
    echo "2. 重启服务："
    echo "   cd $PROJECT_ROOT"
    echo "   docker-compose -f docker/docker-compose.yml down"
    echo "   docker-compose -f docker/docker-compose.yml up -d"
    echo ""
else
    echo "❌ 镜像构建失败"
    exit 1
fi