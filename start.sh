#!/bin/bash
#===============================================================================
# OpenAI 兼容 API 服务 一键启动脚本
#
# 功能:
#   1. 安装 Python 依赖 (FastAPI + Uvicorn)
#   2. 启动 API 服务 (端口 8080)
#   3. 安装 cloudflared (通过 GitHub 镜像代理)
#   4. 启动 Cloudflare 隧道 (公网 HTTPS 访问，无安全提示)
#
# 使用: chmod +x start.sh && ./start.sh
#===============================================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# 配置
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_PORT=8080
CLOUDFLARED_BIN="/usr/local/bin/cloudflared"
API_LOG="/tmp/api-server.log"
CF_LOG="/tmp/cloudflared.log"

print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
print_step()    { echo -e "\n${CYAN}========== $1 ==========${NC}"; }

#===============================================================================
# 步骤 1: 安装 Python 依赖
#===============================================================================
print_step "步骤 1/4: 安装 Python 依赖"

if ! python3 -c "import fastapi" 2>/dev/null; then
    print_info "安装 FastAPI + Uvicorn..."
    pip install -q fastapi uvicorn pydantic 2>&1 | tail -3
fi
print_success "Python 依赖就绪"

#===============================================================================
# 步骤 2: 启动 API 服务
#===============================================================================
print_step "步骤 2/4: 启动 API 服务"

# 停止已有服务
pkill -f "python3.*main.py" 2>/dev/null || true
sleep 1

# 启动服务
cd "${SCRIPT_DIR}"
setsid python3 main.py > "${API_LOG}" 2>&1 &
API_PID=$!
echo $API_PID > /tmp/api-server.pid

sleep 3
if curl -s http://localhost:${API_PORT}/health | grep -q "ok"; then
    print_success "API 服务已启动 (PID=${API_PID}, 端口=${API_PORT})"
else
    print_error "API 服务启动失败"
    cat "${API_LOG}"
    exit 1
fi

#===============================================================================
# 步骤 3: 安装 cloudflared
#===============================================================================
print_step "步骤 3/4: 安装 cloudflared"

if [ -f "${CLOUDFLARED_BIN}" ] && ${CLOUDFLARED_BIN} version &>/dev/null; then
    print_success "cloudflared 已安装: $(${CLOUDFLARED_BIN} version 2>&1)"
else
    print_info "下载 cloudflared..."
    ARCH=$(uname -m)
    case "${ARCH}" in
        x86_64)  CFA_FILE="cloudflared-linux-amd64" ;;
        aarch64) CFA_FILE="cloudflared-linux-arm64" ;;
        armv7l)  CFA_FILE="cloudflared-linux-arm" ;;
        *) print_error "不支持的架构: ${ARCH}"; exit 1 ;;
    esac

    GITHUB_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/${CFA_FILE}"
    DOWNLOAD_OK=false
    for mirror in "https://ghfast.top" "https://gh-proxy.com" "https://mirror.ghproxy.com" ""; do
        url="${mirror:+${mirror}/}${GITHUB_URL}"
        print_info "尝试: ${url}"
        if curl -fSL -o /tmp/cloudflared --connect-timeout 10 --max-time 120 "$url" 2>/dev/null; then
            SIZE=$(stat -c%s /tmp/cloudflared 2>/dev/null)
            if [ "$SIZE" -gt 10000000 ]; then
                chmod +x /tmp/cloudflared
                mv /tmp/cloudflared "${CLOUDFLARED_BIN}"
                DOWNLOAD_OK=true
                print_success "cloudflared 安装完成 ($((SIZE/1024/1024)) MB)"
                break
            fi
        fi
    done

    if [ "${DOWNLOAD_OK}" = false ]; then
        print_error "cloudflared 下载失败，请检查网络连接"
        exit 1
    fi
fi

#===============================================================================
# 步骤 4: 启动 Cloudflare 隧道
#===============================================================================
print_step "步骤 4/4: 启动 Cloudflare 隧道"

pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

setsid ${CLOUDFLARED_BIN} tunnel --url "http://localhost:${API_PORT}" > "${CF_LOG}" 2>&1 &
CF_PID=$!
echo $CF_PID > /tmp/cloudflared.pid

print_info "等待隧道建立..."
sleep 10

TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "${CF_LOG}" 2>/dev/null | head -1)

if [ -n "${TUNNEL_URL}" ]; then
    print_success "Cloudflare 隧道已建立 (PID=${CF_PID})"
else
    print_warn "隧道 URL 尚未出现，请稍后查看日志: ${CF_LOG}"
    TUNNEL_URL="(请查看 ${CF_LOG} 获取地址)"
fi

#===============================================================================
# 完成
#===============================================================================
print_step "启动完成!"

echo -e """
${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}
${GREEN}║          OpenAI 兼容 API 服务已就绪!                          ║${NC}
${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}
${GREEN}║${NC}  本地访问:   http://localhost:${API_PORT}                       ${GREEN}║${NC}
${GREEN}║${NC}  外网访问:   ${TUNNEL_URL}  ${GREEN}║${NC}
${GREEN}║${NC}  API文档:   http://localhost:${API_PORT}/docs                  ${GREEN}║${NC}
${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}
${GREEN}║${NC}  接口列表:                                                   ${GREEN}║${NC}
${GREEN}║${NC}    POST /v1/chat/completions  (OpenAI 兼容聊天接口)        ${GREEN}║${NC}
${GREEN}║${NC}    GET  /v1/models            (模型列表)                    ${GREEN}║${NC}
${GREEN}║${NC}    POST /process              (通用处理接口)                ${GREEN}║${NC}
${GREEN}║${NC}    GET  /health               (健康检查)                    ${GREEN}║${NC}
${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}
${GREEN}║${NC}  停止服务:   kill ${API_PID} ${CF_PID}                            ${GREEN}║${NC}
${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}
"""

echo -e "${YELLOW}提示:${NC}"
echo -e "  - Cloudflare 隧道提供受信任的 HTTPS 地址，浏览器无安全提示"
echo -e "  - 快速隧道 URL 每次启动会变化"
echo -e "  - 修改 main.py 中的 process_request() 可自定义处理逻辑"
echo -e "  - 重新运行本脚本可重启所有服务"
