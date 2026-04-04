#!/usr/bin/env bash
# ============================================================
#  freeswitch_check.sh
#  FreeSWITCH 启动前检查 + 常见报错修复指南
#
#  用法：
#    chmod +x freeswitch_check.sh
#    ./freeswitch_check.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'; YLW='\033[1;33m'; GRN='\033[0;32m'; BLU='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "  ${GRN}✓${NC}  $*"; }
warn() { echo -e "  ${YLW}⚠${NC}  $*"; }
err()  { echo -e "  ${RED}✗${NC}  $*"; }
info() { echo -e "  ${BLU}→${NC}  $*"; }

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  FreeSWITCH 启动检查"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

CONF_DIR="$(cd "$(dirname "$0")" && pwd)/freeswitch/conf"
DOCKER_COMPOSE="$(cd "$(dirname "$0")" && pwd)/docker/docker-compose.yml"

# ─────────────────────────────────────────────────────────────
# 1. 配置文件检查
# ─────────────────────────────────────────────────────────────
echo ""
echo "【1】配置文件检查"

REQUIRED_CONF=(
    "vars.xml"
    "autoload_configs/modules.conf.xml"
    "autoload_configs/event_socket.conf.xml"
    "autoload_configs/sofia.conf.xml"
    "autoload_configs/acl.conf.xml"
    "autoload_configs/cdr_csv.conf.xml"
    "autoload_configs/logfile.conf.xml"
    "autoload_configs/local_stream.conf.xml"
    "dialplan/outbound.xml"
    "directory/default.xml"
)

all_ok=true
for f in "${REQUIRED_CONF[@]}"; do
    if [[ -f "$CONF_DIR/$f" ]]; then
        ok "$f"
    else
        err "$f  ← 文件不存在！"
        all_ok=false
    fi
done

# XML 语法检查（需要 xmllint）
if command -v xmllint &>/dev/null; then
    echo ""
    echo "  XML 语法验证："
    for f in "${REQUIRED_CONF[@]}"; do
        fp="$CONF_DIR/$f"
        [[ -f "$fp" ]] || continue
        if xmllint --noout "$fp" 2>/dev/null; then
            ok "XML 合法: $f"
        else
            err "XML 错误: $f"
            xmllint --noout "$fp" 2>&1 | sed 's/^/     /'
            all_ok=false
        fi
    done
else
    warn "xmllint 未安装，跳过 XML 验证（brew install libxml2 / apt install libxml2-utils）"
fi

# ─────────────────────────────────────────────────────────────
# 2. 必要目录检查
# ─────────────────────────────────────────────────────────────
echo ""
echo "【2】目录检查"

DIRS=("logs/freeswitch" "logs/backend")
for d in "${DIRS[@]}"; do
    dir="$(dirname "$0")/$d"
    if [[ -d "$dir" ]]; then
        ok "目录存在: $d"
    else
        mkdir -p "$dir"
        ok "目录已创建: $d"
    fi
done

# ─────────────────────────────────────────────────────────────
# 3. Docker 环境检查
# ─────────────────────────────────────────────────────────────
echo ""
echo "【3】Docker 环境检查"

if ! command -v docker &>/dev/null; then
    err "Docker 未安装"; exit 1
else
    ok "Docker: $(docker --version | cut -d' ' -f3 | tr -d ',')"
fi

if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
    err "docker-compose 未安装"
else
    ok "docker-compose 可用"
fi

# 检查 host 网络模式（Linux 才支持，macOS/Windows 不支持）
OS=$(uname -s)
if [[ "$OS" != "Linux" ]]; then
    warn "当前系统：$OS"
    warn "network_mode: host 仅在 Linux 上生效！"
    warn "macOS / Windows 上 FreeSWITCH 容器需要改用 bridge 网络 + 端口映射"
    info "macOS 替代方案见脚本末尾"
fi

# ─────────────────────────────────────────────────────────────
# 4. 端口占用检查
# ─────────────────────────────────────────────────────────────
echo ""
echo "【4】端口占用检查"

check_port() {
    local port=$1; local name=$2
    if lsof -i ":$port" &>/dev/null 2>&1 || ss -tlnp "sport = :$port" 2>/dev/null | grep -q LISTEN; then
        warn "端口 $port ($name) 已被占用，可能冲突"
    else
        ok "端口 $port ($name) 空闲"
    fi
}

check_port 5060 "SIP internal"
check_port 5080 "SIP outbound"
check_port 8021 "ESL Inbound"
check_port 9999 "ESL Socket Server"
check_port 8000 "Backend API"

# ─────────────────────────────────────────────────────────────
# 5. 镜像可用性检查
# ─────────────────────────────────────────────────────────────
echo ""
echo "【5】Docker 镜像检查"

if docker image inspect drachtio/freeswitch-modules:latest &>/dev/null 2>&1; then
    ok "drachtio/freeswitch-modules:latest 已缓存"
else
    warn "镜像未缓存，首次启动需要拉取（约 500MB）"
    info "可提前执行：docker pull drachtio/freeswitch-modules:latest"
fi

# ─────────────────────────────────────────────────────────────
# 6. 验证 modules.conf.xml 没有危险模块
# ─────────────────────────────────────────────────────────────
echo ""
echo "【6】模块配置安全检查"

MODULES_FILE="$CONF_DIR/autoload_configs/modules.conf.xml"
if [[ -f "$MODULES_FILE" ]]; then
    # 检查是否有注释掉的危险模块仍然被加载
    DANGEROUS=(
        "mod_python" "mod_python3"   # Python 路径问题
        "mod_audio_stream"            # stock 镜像无此模块
        "mod_record"                  # 不是独立模块名
    )
    for m in "${DANGEROUS[@]}"; do
        # 查找未被注释的加载指令
        if grep -E "^\s*<load module=\"$m\"" "$MODULES_FILE" &>/dev/null; then
            err "危险：$m 未注释但 stock 镜像可能没有此模块"
            info "解决方法：在 modules.conf.xml 中注释掉该行"
        else
            ok "$m 已正确注释或未加载"
        fi
    done
fi

# ─────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  启动命令："
echo ""
echo "    # 仅启动 FreeSWITCH（测试配置用）"
echo "    docker-compose -f docker/docker-compose.yml up freeswitch"
echo ""
echo "    # 启动全部服务"
echo "    docker-compose -f docker/docker-compose.yml up -d"
echo ""
echo "    # 查看 FreeSWITCH 实时日志"
echo "    docker logs -f outbound_freeswitch"
echo ""
echo "    # 进入 FreeSWITCH 控制台"
echo "    docker exec -it outbound_freeswitch fs_cli"
echo ""
echo "    # 检查模块是否加载成功"
echo "    docker exec outbound_freeswitch fs_cli -x 'module_exists mod_sofia'"
echo "    docker exec outbound_freeswitch fs_cli -x 'sofia status'"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# macOS 提示
if [[ "$(uname -s)" == "Darwin" ]]; then
echo ""
cat << 'MACOS_TIP'
  ⚠  macOS 用户：network_mode: host 不支持
  请改用以下配置替换 docker-compose.yml 中的 freeswitch 服务：

    freeswitch:
      image: drachtio/freeswitch-modules:latest
      # 去掉 network_mode: host，改为显式端口映射
      ports:
        - "5060:5060/udp"    # SIP
        - "5080:5080/udp"    # SIP outbound
        - "8021:8021"         # ESL
        - "16384-16484:16384-16484/udp"  # RTP（缩小范围）
      environment:
        JACK_NO_AUDIO_RESERVATION: "1"
      # 同时在 vars.xml 中把 external_rtp_ip / external_sip_ip
      # 设为 host.docker.internal（容器访问宿主机的方式）

MACOS_TIP
fi

echo ""
