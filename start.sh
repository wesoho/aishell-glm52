#!/bin/bash
#===============================================================================
# OpenAI 兼容 API 服务 一键启动脚本  v2.0
#
# 用法:
#   ./start.sh              启动服务 (API + Cloudflare 隧道)
#   ./start.sh stop         停止所有服务
#   ./start.sh restart      重启服务
#   ./start.sh status       查看运行状态
#   ./start.sh --help       显示帮助
#
# 环境变量:
#   API_PORT      API 服务端口 (默认 8080)
#   NO_TUNNEL     设为 1 则不启动 Cloudflare 隧道
#===============================================================================

set -euo pipefail

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

# ── 配置 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_PORT="${API_PORT:-8080}"
CLOUDFLARED_BIN="/usr/local/bin/cloudflared"
API_LOG="/tmp/api-server.log"
CF_LOG="/tmp/cloudflared.log"
API_PID_FILE="/tmp/api-server.pid"
CF_PID_FILE="/tmp/cloudflared.pid"

# ── 输出函数 ──
print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
print_step()    { echo -e "\n${CYAN}═══ $1 ═══${NC}"; }

# ── 帮助 ──
show_help() {
    cat <<EOF
OpenAI 兼容 API 服务启动脚本

用法:
  ./start.sh              启动服务 (API + Cloudflare 隧道)
  ./start.sh stop         停止所有服务
  ./start.sh restart      重启服务
  ./start.sh status       查看运行状态
  ./start.sh --help       显示此帮助

环境变量:
  API_PORT      API 服务端口 (默认: 8080)
  NO_TUNNEL     设为 1 则不启动隧道 (默认: 启动)

接口:
  POST /v1/chat/completions   OpenAI 兼容聊天接口 (支持 stream)
  POST /v1/completions        OpenAI 文本补全接口
  GET  /v1/models             模型列表
  POST /process               通用处理接口
  GET  /health                健康检查
  GET  /docs                  Swagger 文档
EOF
    exit 0
}

# ── 停止服务 ──
stop_services() {
    print_step "停止服务"
    local stopped=0
    for pidfile in "$API_PID_FILE" "$CF_PID_FILE"; do
        if [ -f "$pidfile" ]; then
            local pid
            pid=$(cat "$pidfile" 2>/dev/null || true)
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
                print_info "已停止 PID=$pid"
                stopped=1
            fi
            rm -f "$pidfile"
        fi
    done
    # 兜底：按进程名清理
    pkill -f "python3.*main.py" 2>/dev/null && stopped=1 || true
    pkill -f "cloudflared tunnel" 2>/dev/null && stopped=1 || true
    [ $stopped -eq 1 ] && print_success "服务已停止" || print_info "没有运行中的服务"
}

# ── 查看状态 ──
show_status() {
    print_step "运行状态"
    local api_ok=false cf_ok=false

    if curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
        local health
        health=$(curl -s "http://localhost:${API_PORT}/health")
        print_success "API 服务: 运行中 (端口 ${API_PORT}) — ${health}"
        api_ok=true
    else
        print_error "API 服务: 未运行"
    fi

    if [ -f "$CF_PID_FILE" ] && kill -0 "$(cat "$CF_PID_FILE")" 2>/dev/null; then
        local url
        url=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | head -1)
        print_success "Cloudflare 隧道: 运行中 — ${url:-URL解析中...}"
        cf_ok=true
    else
        print_error "Cloudflare 隧道: 未运行"
    fi

    $api_ok && $cf_ok && return 0 || return 1
}

# ── 子命令处理 ──
case "${1:-}" in
    --help|-h) show_help ;;
    stop)      stop_services; exit 0 ;;
    status)    show_status; exit $? ;;
    restart)   stop_services; sleep 1 ;;
esac

#===============================================================================
# 启动流程
#===============================================================================

# ── 端口检查 ──
if curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
    print_warn "端口 ${API_PORT} 已被占用，先停止旧服务..."
    stop_services
    sleep 2
fi

# ── 步骤 1: 安装 Python 依赖 ──
print_step "步骤 1: 安装 Python 依赖"
if ! python3 -c "import fastapi, uvicorn" 2>/dev/null; then
    print_info "安装 FastAPI + Uvicorn..."
    pip install -q fastapi uvicorn pydantic 2>&1 | tail -5
else
    print_success "Python 依赖已就绪"
fi

# ── 步骤 2: 启动 API 服务 ──
print_step "步骤 2: 启动 API 服务 (端口 ${API_PORT})"

cd "${SCRIPT_DIR}"
API_PORT="${API_PORT}" setsid python3 main.py > "${API_LOG}" 2>&1 &
API_PID=$!
echo "$API_PID" > "$API_PID_FILE"

# 等待服务就绪 (最多 15 秒)
for i in $(seq 1 15); do
    if curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
        print_success "API 服务已启动 (PID=${API_PID})"
        break
    fi
    [ $i -eq 15 ] && { print_error "API 服务启动超时"; cat "${API_LOG}" | tail -20; exit 1; }
    sleep 1
done

# ── 步骤 3: 启动 Cloudflare 隧道 ──
TUNNEL_URL=""

if [ "${NO_TUNNEL:-0}" = "1" ]; then
    print_step "跳过隧道 (NO_TUNNEL=1)"
else
    print_step "步骤 3: 启动 Cloudflare 隧道"

    # 安装 cloudflared (如未安装)
    if [ ! -x "${CLOUDFLARED_BIN}" ] || ! "${CLOUDFLARED_BIN}" version &>/dev/null; then
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
            if curl -fSL -o /tmp/cloudflared_dl --connect-timeout 10 --max-time 120 "$url" 2>/dev/null; then
                SIZE=$(stat -c%s /tmp/cloudflared_dl 2>/dev/null || echo 0)
                if [ "$SIZE" -gt 10000000 ]; then
                    chmod +x /tmp/cloudflared_dl
                    cp /tmp/cloudflared_dl "${CLOUDFLARED_BIN}"
                    rm -f /tmp/cloudflared_dl
                    DOWNLOAD_OK=true
                    print_success "cloudflared 安装完成 ($((SIZE/1024/1024)) MB)"
                    break
                fi
            fi
        done
        [ "${DOWNLOAD_OK}" = false ] && { print_error "cloudflared 下载失败"; exit 1; }
    else
        print_success "cloudflared 已安装"
    fi

    # 启动隧道
    pkill -f "cloudflared tunnel" 2>/dev/null || true
    sleep 1

    setsid "${CLOUDFLARED_BIN}" tunnel --url "http://localhost:${API_PORT}" > "${CF_LOG}" 2>&1 &
    CF_PID=$!
    echo "$CF_PID" > "$CF_PID_FILE"

    # 等待隧道 URL (最多 20 秒)
    print_info "等待隧道建立..."
    for i in $(seq 1 20); do
        TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "${CF_LOG}" 2>/dev/null | head -1)
        [ -n "${TUNNEL_URL}" ] && break
        sleep 1
    done

    if [ -n "${TUNNEL_URL}" ]; then
        print_success "隧道已建立 (PID=${CF_PID})"
    else
        print_warn "隧道 URL 尚未出现，查看日志: ${CF_LOG}"
        TUNNEL_URL="(请查看 ${CF_LOG})"
    fi
fi

# ── 完成 ──
print_step "启动完成!"

LOCAL_URL="http://localhost:${API_PORT}"
echo ""
echo -e "  ${GREEN}本地访问:${NC}  ${LOCAL_URL}"
echo -e "  ${GREEN}外网访问:${NC}  ${TUNNEL_URL:-未启动隧道}"
echo -e "  ${GREEN}API 文档:${NC}  ${LOCAL_URL}/docs"
echo ""
echo -e "  ${CYAN}接口列表:${NC}"
echo -e "    POST /v1/chat/completions   OpenAI 兼容聊天 (支持 stream)"
echo -e "    POST /v1/completions        OpenAI 文本补全"
echo -e "    GET  /v1/models             模型列表"
echo -e "    POST /process               通用处理接口"
echo -e "    GET  /health                健康检查"
echo ""
echo -e "  ${YELLOW}停止服务:${NC}  ./start.sh stop"
echo -e "  ${YELLOW}查看状态:${NC}  ./start.sh status"
echo -e "  ${YELLOW}重启服务:${NC}  ./start.sh restart"
echo ""
echo -e "  ${YELLOW}提示:${NC} 修改 main.py 中的 process_request() 可自定义处理逻辑"
