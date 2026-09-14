#!/bin/bash
#===============================================================================
# OpenAI 兼容 API 服务 一键启动脚本  v2.2
#
# 用法:
#   ./start.sh              启动服务 (API + Cloudflare 隧道)
#   ./start.sh stop         停止所有服务
#   ./start.sh restart      重启服务
#   ./start.sh status       查看运行状态
#   ./start.sh logs         查看日志 (请求日志 + API 日志 + 隧道日志)
#   ./start.sh logs -f      实时跟踪日志
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
BOLD='\033[1m'; DIM='\033[2m'

# ── 配置 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_PORT="${API_PORT:-8080}"
CLOUDFLARED_BIN="/usr/local/bin/cloudflared"
API_LOG="/tmp/api-server.log"
CF_LOG="/tmp/cloudflared.log"
REQ_LOG="/tmp/api-requests.log"
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
  ./start.sh logs         查看最近日志 (请求 + API + 隧道)
  ./start.sh logs -f      实时跟踪日志 (Ctrl+C 退出)
  ./start.sh --help       显示此帮助

环境变量:
  API_PORT      API 服务端口 (默认: 8080)
  NO_TUNNEL     设为 1 则不启动隧道 (默认: 启动)

日志文件:
  /tmp/api-requests.log       请求日志 (每个请求的方法/路径/IP/耗时/状态)
  /tmp/api-server.log         API 服务日志 (启动/处理/错误)
  /tmp/cloudflared.log         隧道日志 (连接/断开/错误)
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
        url=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | head -1 || true)
        print_success "Cloudflare 隧道: 运行中 — ${url:-URL解析中...}"
        cf_ok=true
    else
        print_error "Cloudflare 隧道: 未运行"
    fi

    $api_ok && $cf_ok && return 0 || return 1
}

# ── 查看日志 ──
show_logs() {
    local follow="${1:-}"
    local lines=50

    if [ "$follow" = "-f" ]; then
        print_step "实时跟踪日志 (Ctrl+C 退出)"
        echo -e "  ${DIM}请求日志: ${REQ_LOG}${NC}"
        echo -e "  ${DIM}API 日志:  ${API_LOG}${NC}"
        echo -e "  ${DIM}隧道日志:  ${CF_LOG}${NC}"
        echo ""
        tail -f "$REQ_LOG" "$API_LOG" "$CF_LOG" 2>/dev/null
        exit 0
    fi

    print_step "请求日志 (最近 ${lines} 行)"
    if [ -f "$REQ_LOG" ]; then
        tail -n "$lines" "$REQ_LOG"
    else
        echo -e "  ${DIM}(暂无请求日志)${NC}"
    fi

    echo ""
    print_step "API 服务日志 (最近 ${lines} 行)"
    if [ -f "$API_LOG" ]; then
        tail -n "$lines" "$API_LOG"
    else
        echo -e "  ${DIM}(暂无 API 日志)${NC}"
    fi

    echo ""
    print_step "隧道日志 (最近 20 行)"
    if [ -f "$CF_LOG" ]; then
        tail -n 20 "$CF_LOG"
    else
        echo -e "  ${DIM}(暂无隧道日志)${NC}"
    fi
    exit 0
}

# ── 子命令处理 ──
case "${1:-}" in
    --help|-h) show_help ;;
    stop)      stop_services; exit 0 ;;
    status)    show_status; exit $? ;;
    logs)      show_logs "${2:-}"; exit 0 ;;
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
print_step "步骤 1/3: 安装 Python 依赖"
if ! python3 -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    print_info "安装 FastAPI + Uvicorn..."
    pip install -q fastapi uvicorn pydantic httpx 2>&1 | tail -5
else
    print_success "Python 依赖已就绪"
fi

# ── 步骤 2: 启动 API 服务 ──
print_step "步骤 2/3: 启动 API 服务 (端口 ${API_PORT})"

cd "${SCRIPT_DIR}"
API_PORT="${API_PORT}" setsid python3 main.py > "${API_LOG}" 2>&1 &
API_PID=$!
echo "$API_PID" > "$API_PID_FILE"

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
HAS_TUNNEL=false

if [ "${NO_TUNNEL:-0}" = "1" ]; then
    print_step "跳过隧道 (NO_TUNNEL=1)"
else
    print_step "步骤 3/3: 启动 Cloudflare 隧道"

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

    pkill -f "cloudflared tunnel" 2>/dev/null || true
    sleep 1

    setsid "${CLOUDFLARED_BIN}" tunnel --url "http://localhost:${API_PORT}" > "${CF_LOG}" 2>&1 &
    CF_PID=$!
    echo "$CF_PID" > "$CF_PID_FILE"

    print_info "等待隧道建立..."
    for i in $(seq 1 30); do
        TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "${CF_LOG}" 2>/dev/null | head -1 || true)
        if [ -n "${TUNNEL_URL}" ]; then
            break
        fi
        sleep 1
    done

    if [ -n "${TUNNEL_URL}" ]; then
        print_success "隧道已建立 (PID=${CF_PID})"
        HAS_TUNNEL=true
    else
        print_warn "隧道 URL 尚未出现，查看日志: ${CF_LOG}"
        TUNNEL_URL="(请查看 ${CF_LOG})"
    fi
fi

#===============================================================================
# 启动完成 — 打印使用说明
#===============================================================================

LOCAL_URL="http://localhost:${API_PORT}"
if [ "$HAS_TUNNEL" = true ]; then
    PUBLIC_URL="${TUNNEL_URL}"
else
    PUBLIC_URL="${LOCAL_URL}"
fi

echo ""
echo -e "${GREEN}${BOLD}┌─────────────────────────────────────────────────────────┐${NC}"
echo -e "${GREEN}${BOLD}│          ✅  服务启动成功，可以开始使用了！             │${NC}"
echo -e "${GREEN}${BOLD}└─────────────────────────────────────────────────────────┘${NC}"
echo ""

echo -e "${CYAN}${BOLD}📍 访问地址${NC}"
echo -e "   ${GREEN}本地:${NC}  ${LOCAL_URL}"
if [ "$HAS_TUNNEL" = true ]; then
    echo -e "   ${GREEN}外网:${NC}  ${PUBLIC_URL}"
    echo -e "   ${DIM}(外网 HTTPS 地址，每次重启会变化)${NC}"
fi
echo -e "   ${GREEN}文档:${NC}  ${LOCAL_URL}/docs"
echo ""

echo -e "${CYAN}${BOLD}🔧 快速测试 (复制即可运行)${NC}"
echo ""
echo -e "${DIM}# 1. 健康检查${NC}"
echo -e "   ${YELLOW}curl ${LOCAL_URL}/health${NC}"
echo ""
echo -e "${DIM}# 2. 聊天对话${NC}"
echo -e "   ${YELLOW}curl ${LOCAL_URL}/v1/chat/completions \\${NC}"
echo -e "   ${YELLOW}  -H \"Content-Type: application/json\" \\${NC}"
echo -e "   ${YELLOW}  -d '{\"model\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'${NC}"
echo ""
echo -e "${DIM}# 3. 查看模型列表${NC}"
echo -e "   ${YELLOW}curl ${LOCAL_URL}/v1/models${NC}"
echo ""

echo -e "${CYAN}${BOLD}🐍 Python 调用 (OpenAI SDK)${NC}"
echo ""
echo -e "   ${DIM}from openai import OpenAI${NC}"
echo ""
echo -e "   ${DIM}client = OpenAI(${NC}"
echo -e "   ${DIM}    base_url=\"${PUBLIC_URL}/v1\",${NC}"
echo -e "   ${DIM}    api_key=\"any\"          # 不校验，随便填${NC}"
echo -e "   ${DIM})${NC}"
echo ""
echo -e "   ${DIM}# 普通调用${NC}"
echo -e "   ${DIM}resp = client.chat.completions.create(${NC}"
echo -e "   ${DIM}    model=\"default\",${NC}"
echo -e "   ${DIM}    messages=[{\"role\": \"user\", \"content\": \"你好\"}]${NC}"
echo -e "   ${DIM})${NC}"
echo -e "   ${DIM}print(resp.choices[0].message.content)${NC}"
echo ""
echo -e "   ${DIM}# 流式调用${NC}"
echo -e "   ${DIM}for chunk in client.chat.completions.create(${NC}"
echo -e "   ${DIM}    model=\"default\",${NC}"
echo -e "   ${DIM}    messages=[{\"role\": \"user\", \"content\": \"你好\"}],${NC}"
echo -e "   ${DIM}    stream=True${NC}"
echo -e "   ${DIM}):${NC}"
echo -e "   ${DIM}    if chunk.choices[0].delta.content:${NC}"
echo -e "   ${DIM}        print(chunk.choices[0].delta.content, end=\"\")${NC}"
echo ""

echo -e "${CYAN}${BOLD}🖥️  Cursor / VS Code 配置${NC}"
echo ""
echo -e "   在设置中填入以下信息即可对接："
echo ""
echo -e "   ${GREEN}API Base URL:${NC}  ${PUBLIC_URL}/v1"
echo -e "   ${GREEN}API Key:${NC}       any (不校验，随便填)"
echo -e "   ${GREEN}Model:${NC}          default"
echo ""

echo -e "${CYAN}${BOLD}📋 接口一览${NC}"
echo ""
echo -e "   ${BLUE}POST${NC} /v1/chat/completions   OpenAI 兼容聊天 (支持 stream)"
echo -e "   ${BLUE}POST${NC} /v1/completions        OpenAI 文本补全"
echo -e "   ${BLUE}GET ${NC} /v1/models             模型列表"
echo -e "   ${BLUE}POST${NC} /process               通用处理接口"
echo -e "   ${BLUE}GET ${NC} /health                健康检查"
echo -e "   ${BLUE}GET ${NC} /docs                  Swagger 交互式文档"
echo ""

echo -e "${CYAN}${BOLD}⚙️  服务管理${NC}"
echo ""
echo -e "   ${YELLOW}./start.sh status${NC}    查看运行状态"
echo -e "   ${YELLOW}./start.sh stop${NC}      停止所有服务"
echo -e "   ${YELLOW}./start.sh restart${NC}   重启服务"
echo -e "   ${YELLOW}./start.sh logs${NC}       查看日志 (请求/API/隧道)"
echo -e "   ${YELLOW}./start.sh logs -f${NC}    实时跟踪日志"
echo ""

echo -e "${CYAN}${BOLD}💡 自定义处理逻辑${NC}"
echo ""
echo -e "   修改 ${BOLD}main.py${NC} 中的上游模型配置和请求处理"
echo -e "   当前为代理模式，直接转发到华为云内置 GLM 模型"
echo -e "   修改后执行 ${YELLOW}./start.sh restart${NC} 生效"
echo ""
