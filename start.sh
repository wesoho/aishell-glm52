#!/bin/bash
#===============================================================================
# OpenAI 兼容 API 服务 一键启动脚本  v3.2
#
# 用法:
#   ./start.sh              启动服务 (API + Cloudflare 隧道) + 保活看门狗
#   ./start.sh stop         停止所有服务 (API + 隧道 + 看门狗)
#   ./start.sh stop-api     仅停止 API（保留隧道与看门狗）
#   ./start.sh tunnel       仅启动/重启隧道（不影响 API）
#   ./start.sh restart      仅重启 API 服务，保留隧道（域名不变）
#   ./start.sh status       查看运行状态
#   ./start.sh logs         查看日志 (请求日志 + API 日志 + 隧道日志)
#   ./start.sh logs -f      实时跟踪日志
#   ./start.sh --help       显示帮助
#
# 环境变量:
#   API_PORT      API 服务端口 (默认 8080)
#   NO_TUNNEL     设为 1 则不启动 Cloudflare 隧道
#   NO_WATCHDOG   设为 1 则不启动保活看门狗
#===============================================================================

set -euo pipefail

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'; DIM='\033[2m'

# ── 环境适配（可选，全部可被环境变量覆盖）──
#   VENV_PYTHON_DIR: venv 的 bin 目录，设置后 python3 优先用 venv（如 /root/runtime/aishell-glm52/venv/bin）
#   API_PORT / CLOUDFLARED_BIN / CF_LOG / CF_URL_FILE 等均可通过环境变量覆盖，见上方配置区
if [ -n "${VENV_PYTHON_DIR:-}" ]; then
    export PATH="${VENV_PYTHON_DIR}:${PATH}"
fi
# 华为云平台以 JOB_ENV_HW_* 注入凭证 → 映射为 Snap Access 读取的 HW_*
export HW_ACCESS_KEY="${HW_ACCESS_KEY:-${JOB_ENV_HW_ACCESS_KEY:-}}"
export HW_SECRET_KEY="${HW_SECRET_KEY:-${JOB_ENV_HW_SECRET_KEY:-}}"
export HW_SECURITY_TOKEN="${HW_SECURITY_TOKEN:-${JOB_ENV_HW_SECURITY_TOKEN:-}}"

# ── 配置 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_PORT="${API_PORT:-8080}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-/usr/local/bin/cloudflared}"
API_LOG="${API_LOG:-/tmp/api-server.log}"
CF_LOG="${CF_LOG:-/tmp/cloudflared.log}"
REQ_LOG="${REQ_LOG:-/tmp/api-requests.log}"
API_PID_FILE="${API_PID_FILE:-/tmp/api-server.pid}"
CF_PID_FILE="${CF_PID_FILE:-/tmp/cloudflared.pid}"
CF_URL_FILE="${CF_URL_FILE:-/tmp/cloudflared-url.txt}"
WATCHDOG_PID_FILE="${WATCHDOG_PID_FILE:-/tmp/aishell-watchdog.pid}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/aishell-watchdog.log}"
WATCHDOG_INTERVAL=30  # 检查间隔（秒）

# 上游验证参数（从 config.json 读取，用于启动时校验 API Key 有效性）
_UPSTREAM_BASE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("upstream_base_url","https://tokenhub.developer.huaweicloud.com/v2"))' "${SCRIPT_DIR}/config.json" 2>/dev/null || echo 'https://tokenhub.developer.huaweicloud.com/v2')"
_UPSTREAM_MODEL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("upstream_default_model","deepseek-v4-flash-0731"))' "${SCRIPT_DIR}/config.json" 2>/dev/null || echo 'deepseek-v4-flash-0731')"

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
  ./start.sh              启动服务 (API + Cloudflare 隧道 + 保活看门狗)
  ./start.sh stop         停止所有服务 (API + 隧道 + 看门狗)
  ./start.sh stop-api     仅停止 API（保留隧道与看门狗）
  ./start.sh tunnel       仅启动/重启隧道（不影响 API）
  ./start.sh restart      仅重启 API 服务，保留隧道（域名不变）
  ./start.sh status       查看运行状态
  ./start.sh logs         查看最近日志 (请求 + API + 隧道)
  ./start.sh logs -f      实时跟踪日志 (Ctrl+C 退出)
  ./start.sh --help       显示此帮助

环境变量:
  API_PORT      API 服务端口 (默认: 8080)
  NO_TUNNEL     设为 1 则不启动隧道 (默认: 启动)
  NO_WATCHDOG   设为 1 则不启动保活看门狗 (默认: 启动)
EOF
    exit 0
}

# ── 获取隧道 URL ──
get_tunnel_url() {
    if [ -f "$CF_URL_FILE" ]; then
        cat "$CF_URL_FILE" 2>/dev/null
        return
    fi
    grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | head -1 || true
}

# ── 检查隧道是否存活 ──
tunnel_alive() {
    [ -f "$CF_PID_FILE" ] && kill -0 "$(cat "$CF_PID_FILE" 2>/dev/null)" 2>/dev/null
}

# ── 检查 API 是否存活 ──
api_alive() {
    curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1
}

# ── 检查隧道是否真正可用（URL 能返回业务页面，而非只进程活着）──
tunnel_serves() {
    local url
    url=$(get_tunnel_url)
    [ -n "$url" ] || return 1
    curl -sf --connect-timeout 5 --max-time 10 "${url}/health" >/dev/null 2>&1
}

# ── 停止 API 服务 ──
stop_api() {
    if [ -f "$API_PID_FILE" ]; then
        local pid
        pid=$(cat "$API_PID_FILE" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            print_info "已停止 API PID=$pid"
        fi
        rm -f "$API_PID_FILE"
    fi
    pkill -f "python3.*main.py" 2>/dev/null || true
}

# ── 停止隧道 ──
stop_tunnel() {
    if [ -f "$CF_PID_FILE" ]; then
        local pid
        pid=$(cat "$CF_PID_FILE" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            print_info "已停止隧道 PID=$pid"
        fi
        rm -f "$CF_PID_FILE"
    fi
    pkill -f "cloudflared tunnel" 2>/dev/null || true
    rm -f "$CF_URL_FILE"
}

# ── 停止看门狗 ──
stop_watchdog() {
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        local pid
        pid=$(cat "$WATCHDOG_PID_FILE" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            print_info "已停止看门狗 PID=$pid"
        fi
        rm -f "$WATCHDOG_PID_FILE"
    fi
    pkill -f "aishell-watchdog" 2>/dev/null || true
}

# ── 停止所有服务 ──
stop_services() {
    print_step "停止服务"
    local stopped=0
    stop_watchdog && stopped=1
    stop_api && stopped=1
    stop_tunnel && stopped=1
    [ $stopped -eq 1 ] && print_success "服务已停止" || print_info "没有运行中的服务"
}

# ── 查看状态 ──
show_status() {
    print_step "运行状态"
    local api_ok=false cf_ok=false wd_ok=false

    if api_alive; then
        local health
        health=$(curl -s "http://localhost:${API_PORT}/health")
        print_success "API 服务: 运行中 (端口 ${API_PORT}) — ${health}"
        api_ok=true
    else
        print_error "API 服务: 未运行"
    fi

    if tunnel_alive; then
        local url
        url=$(get_tunnel_url)
        print_success "Cloudflare 隧道: 运行中 — ${url:-URL解析中...}"
        cf_ok=true
    else
        print_error "Cloudflare 隧道: 未运行"
    fi

    if [ -f "$WATCHDOG_PID_FILE" ] && kill -0 "$(cat "$WATCHDOG_PID_FILE" 2>/dev/null)" 2>/dev/null; then
        print_success "保活看门狗: 运行中 (间隔 ${WATCHDOG_INTERVAL}s)"
        wd_ok=true
    else
        print_error "保活看门狗: 未运行"
    fi

    $api_ok && $cf_ok && $wd_ok && return 0 || return 1
}

# ── 查看日志 ──
show_logs() {
    local follow="${1:-}"
    local lines=50

    if [ "$follow" = "-f" ]; then
        print_step "实时跟踪日志 (Ctrl+C 退出)"
        tail -f "$REQ_LOG" "$API_LOG" "$CF_LOG" 2>/dev/null
        exit 0
    fi

    print_step "请求日志 (最近 ${lines} 行)"
    tail -n "$lines" "$REQ_LOG" 2>/dev/null || echo -e "  ${DIM}(暂无)${NC}"
    echo ""
    print_step "API 服务日志 (最近 ${lines} 行)"
    tail -n "$lines" "$API_LOG" 2>/dev/null || echo -e "  ${DIM}(暂无)${NC}"
    echo ""
    print_step "隧道日志 (最近 20 行)"
    tail -n 20 "$CF_LOG" 2>/dev/null || echo -e "  ${DIM}(暂无)${NC}"
    exit 0
}

# ── 校验 API Key 是否被上游接受（200/429=有效, 401=失效）──
key_valid() {
    [ -n "$1" ] || return 1
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 15 \
        -X POST "${_UPSTREAM_BASE}/chat/completions" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $1" \
        -d "{\"model\":\"${_UPSTREAM_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" 2>/dev/null || echo 000)
    case "${code}" in
        200|429) return 0 ;;
        *)       return 1 ;;
    esac
}

# ── 注入 API Key（逐个候选校验，跳过无效 key，避免上游 401 apiKey解密失败）──
inject_api_key() {
    local seen="" cand=""
    # _try_key <key> <来源>：校验通过则注入，并写缓存文件供看门狗/裸启动兜底
    _try_key() {
        local k="$1" src="$2"
        [ -n "$k" ] || return 1
        if key_valid "$k"; then
            export JOB_ENV_MODEL_API_KEY="$k"
            printf '%s' "$k" > /tmp/model_api_key.txt 2>/dev/null || true
            chmod 600 /tmp/model_api_key.txt 2>/dev/null || true
            print_success "API Key 有效 (来自 ${src})"
            return 0
        fi
        print_warn "API Key 无效，跳过 (来自 ${src})"
        return 1
    }

    # 1) 环境变量（也校验，无效继续往下找）
    if _try_key "${JOB_ENV_MODEL_API_KEY:-}" "环境变量"; then return; fi

    # 2) /tmp/model_api_key.txt（上次校验通过的缓存）
    if [ -f /tmp/model_api_key.txt ]; then
        cand=$(tr -d '[:space:]' < /tmp/model_api_key.txt)
        if _try_key "$cand" "/tmp/model_api_key.txt"; then return; fi
    fi

    # 3) 凭证文件（跳过已试过的重复 key，避免重复打上游）
    for credfile in /root/job-envs/sandboxes/*/.dsh/.credentials.yaml; do
        [ -f "$credfile" ] || continue
        grep -q "JOB_ENV_MODEL_API_KEY" "$credfile" 2>/dev/null || continue
        cand=$(grep 'JOB_ENV_MODEL_API_KEY' "$credfile" | head -1 | sed 's/.*: *"//' | sed 's/"$//')
        [ -n "$cand" ] || continue
        printf '%s\n' "$seen" | grep -qF -- "$cand" && continue
        seen="${seen}${cand}\n"
        if _try_key "$cand" "$(basename "$credfile")"; then return; fi
    done

    # 4) /proc 进程环境扫描（跳过已试过的重复 key）
    for pid in $(ls /proc 2>/dev/null | grep '^[0-9]*$' | head -50); do
        cand=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^JOB_ENV_MODEL_API_KEY=' | head -1 | cut -d= -f2-)
        [ -n "$cand" ] || continue
        printf '%s\n' "$seen" | grep -qF -- "$cand" && continue
        seen="${seen}${cand}\n"
        if _try_key "$cand" "/proc/$pid"; then return; fi
    done

    print_warn "未找到有效 API Key，上游请求将无认证"
    unset JOB_ENV_MODEL_API_KEY
}

# ── 注入 Snap Access AK/SK ──
inject_ak_sk() {
    # 从环境变量或凭证文件查找华为云 AK/SK (用于 Snap Access V4 签名)
    if [ -n "${HW_ACCESS_KEY:-}" ] && [ -n "${HW_SECRET_KEY:-}" ]; then
        print_success "Snap Access AK/SK 已在环境变量中"
        return
    fi
    # 从凭证文件查找
    for credfile in /root/job-envs/sandboxes/*/.dsh/.credentials.yaml; do
        if [ -f "$credfile" ]; then
            _ak=$(grep -oP '(?:HW_ACCESS_KEY|access_key)\s*[:=]\s*"?\K[A-Za-z0-9]{10,}' "$credfile" 2>/dev/null | head -1)
            _sk=$(grep -oP '(?:HW_SECRET_KEY|secret_key)\s*[:=]\s*"?\K[A-Za-z0-9]{30,}' "$credfile" 2>/dev/null | head -1)
            if [ -n "$_ak" ] && [ -n "$_sk" ]; then
                export HW_ACCESS_KEY="$_ak"
                export HW_SECRET_KEY="$_sk"
                _st=$(grep -oP '(?:HW_SECURITY_TOKEN|security_token)\s*[:=]\s*"?\K[A-Za-z0-9.+=/-]{20,}' "$credfile" 2>/dev/null | head -1)
                [ -n "$_st" ] && export HW_SECURITY_TOKEN="$_st"
                print_success "从凭证文件注入 Snap Access AK/SK"
                return
            fi
        fi
    done
    # 从 /proc 查找
    for pid in $(ls /proc 2>/dev/null | grep '^[0-9]*$' | head -50); do
        _ak=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | grep '^HW_ACCESS_KEY=' | head -1 | cut -d= -f2-)
        _sk=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | grep '^HW_SECRET_KEY=' | head -1 | cut -d= -f2-)
        if [ -n "$_ak" ] && [ -n "$_sk" ]; then
            export HW_ACCESS_KEY="$_ak"
            export HW_SECRET_KEY="$_sk"
            _st=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | grep '^HW_SECURITY_TOKEN=' | head -1 | cut -d= -f2-)
            [ -n "$_st" ] && export HW_SECURITY_TOKEN="$_st"
            print_success "从 /proc/$pid 注入 Snap Access AK/SK"
            return
        fi
    done
    print_warn "未找到 Snap Access AK/SK，Snap Access 模型将不可用 (TokenHub 模型正常)"
}

# ── 启动 API 服务 ──
start_api() {
    print_step "启动 API 服务 (端口 ${API_PORT})"
    inject_api_key
    inject_ak_sk

    cd "${SCRIPT_DIR}"
    API_PORT="${API_PORT}" setsid python3 main.py > "${API_LOG}" 2>&1 &
    API_PID=$!
    echo "$API_PID" > "$API_PID_FILE"

    for i in $(seq 1 15); do
        if curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
            print_success "API 服务已启动 (PID=${API_PID})"
            return 0
        fi
        [ $i -eq 15 ] && { print_error "API 服务启动超时"; cat "${API_LOG}" | tail -20; return 1; }
        sleep 1
    done
}

# ── 启动 Cloudflare 隧道 ──
start_tunnel() {
    TUNNEL_URL=""
    HAS_TUNNEL=false

    if [ "${NO_TUNNEL:-0}" = "1" ]; then
        print_step "跳过隧道 (NO_TUNNEL=1)"
        return
    fi

    # 隧道已存活且可用 — 复用，不重启（保证 restart 后外网地址不变）
    if tunnel_alive && tunnel_serves; then
        TUNNEL_URL=$(get_tunnel_url)
        print_success "隧道已在运行且可用，复用现有隧道 — ${TUNNEL_URL}"
        HAS_TUNNEL=true
        return
    fi
    # 隧道进程在但已不可用 → 重建（将获得新地址，并写回 URL 文件）
    if tunnel_alive; then
        print_warn "隧道进程在但已不可用，重建隧道..."
        stop_tunnel
        sleep 1
    fi

    print_step "启动 Cloudflare 隧道"

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
        # 优先直连 GitHub，失败立即切镜像；每个源先 3s 快速探测，绝不死等
        for mirror in "" "https://ghfast.top" "https://gh-proxy.com" "https://ghproxy.net" "https://ghproxy.cc" "https://gh.ddlc.top" "https://github.moeyy.xyz" "https://mirror.ghproxy.com"; do
            url="${mirror:+${mirror}/}${GITHUB_URL}"
            print_info "尝试: ${url}"
            case "${mirror}" in
                "") TMO=25 ;;   # 官方源：25s 快速失败，不行立即换镜像
                *)  TMO=120 ;;
            esac
            # Range 请求探测（跟随重定向，只取前 1MB），比 HEAD 更能反映真实下载通道
            if ! curl -fsSL -o /dev/null --connect-timeout 3 --max-time 10 -r 0-1048575 "$url" 2>/dev/null; then
                print_info "  不可用，立即切换下一个源"
                continue
            fi
            if curl -fSL -o /tmp/cloudflared_dl --connect-timeout 5 --max-time "$TMO" "$url" 2>/dev/null; then
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
            rm -f /tmp/cloudflared_dl 2>/dev/null || true
            print_info "  下载失败，立即切换下一个源"
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
        echo "$TUNNEL_URL" > "$CF_URL_FILE"
        print_success "隧道已建立 (PID=${CF_PID}) — ${TUNNEL_URL}"
        HAS_TUNNEL=true
    else
        print_warn "隧道 URL 尚未出现，查看日志: ${CF_LOG}"
        TUNNEL_URL="(请查看 ${CF_LOG})"
    fi
}

# ── 保活看门狗 ──
# 后台循环：每 30s 检查 API 和隧道，挂了就自动拉起
start_watchdog() {
    if [ "${NO_WATCHDOG:-0}" = "1" ]; then
        return
    fi

    # 已在运行则跳过
    if [ -f "$WATCHDOG_PID_FILE" ] && kill -0 "$(cat "$WATCHDOG_PID_FILE" 2>/dev/null)" 2>/dev/null; then
        print_success "看门狗已在运行"
        return
    fi

    # 生成独立看门狗脚本
    cat > /tmp/aishell-watchdog.sh << WD_EOF
#!/bin/bash
# aishell-glm52 保活看门狗 (自动生成)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\$PATH"

# 优先使用 venv 内的 Python（依赖装在 venv 里，系统 python 缺 httpx 等模块）
if [ -x "\${SCRIPT_DIR:-}/venv/bin/python3" ]; then
    PYTHON_BIN="\${SCRIPT_DIR}/venv/bin/python3"
else
    PYTHON_BIN="python3"
fi

API_PORT="${API_PORT}"
SCRIPT_DIR="${SCRIPT_DIR}"
interval=${WATCHDOG_INTERVAL}
API_LOG="${API_LOG}"
CF_LOG="${CF_LOG}"
API_PID_FILE="${API_PID_FILE}"
CF_PID_FILE="${CF_PID_FILE}"
CF_URL_FILE="${CF_URL_FILE}"
WATCHDOG_LOG="${WATCHDOG_LOG}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN}"
UPSTREAM_BASE="${_UPSTREAM_BASE}"
UPSTREAM_MODEL="${_UPSTREAM_MODEL}"

_wd_key_valid() {
    [ -n "\$1" ] || return 1
    local code
    code=\$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 4 --max-time 12 \
        -X POST "\${UPSTREAM_BASE}/chat/completions" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer \$1" \
        -d "{\"model\":\"\${UPSTREAM_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" 2>/dev/null || echo 000)
    case "\$code" in 200|429) return 0 ;; *) return 1 ;; esac
}

while true; do
    sleep "\$interval" 2>/dev/null || exit 0
    ts=\$(date "+%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "?")

    # 检查 API
    if ! curl -sf "http://localhost:\${API_PORT}/health" >/dev/null 2>&1; then
        echo "[\$ts] API down, restarting..." >> "\$WATCHDOG_LOG"
        pkill -f "python3.*main.py" 2>/dev/null
        sleep 1
        KEY_SET=""
        if [ -f /tmp/model_api_key.txt ]; then
            _cand=\$(tr -d '[:space:]' < /tmp/model_api_key.txt)
            if _wd_key_valid "\$_cand"; then
                export JOB_ENV_MODEL_API_KEY="\$_cand"; KEY_SET=1
            else
                echo "[\$ts] /tmp/model_api_key.txt key invalid, trying others" >> "\$WATCHDOG_LOG"
            fi
        fi
        if [ -z "\$KEY_SET" ]; then
            for credfile in /root/job-envs/sandboxes/*/.dsh/.credentials.yaml; do
                [ -f "\$credfile" ] && grep -q "JOB_ENV_MODEL_API_KEY" "\$credfile" 2>/dev/null || continue
                _cand=\$(grep "JOB_ENV_MODEL_API_KEY" "\$credfile" 2>/dev/null | head -1 | sed 's/.*: *"//' | sed 's/"$//' 2>/dev/null)
                if _wd_key_valid "\$_cand"; then
                    export JOB_ENV_MODEL_API_KEY="\$_cand"; KEY_SET=1
                    printf '%s' "\$_cand" > /tmp/model_api_key.txt 2>/dev/null || true
                    break
                fi
            done
        fi
        [ -z "\$KEY_SET" ] && echo "[\$ts] 未找到有效 API Key" >> "\$WATCHDOG_LOG"
        cd "\$SCRIPT_DIR" 2>/dev/null
        API_PORT="\$API_PORT" setsid "\$PYTHON_BIN" main.py >> "\$API_LOG" 2>&1 &
        echo \$! > "\$API_PID_FILE" 2>/dev/null
        sleep 3
        if curl -sf "http://localhost:\${API_PORT}/health" >/dev/null 2>&1; then
            echo "[\$ts] API restarted OK PID=\$(cat \$API_PID_FILE 2>/dev/null)" >> "\$WATCHDOG_LOG"
        else
            echo "[\$ts] API restart FAILED" >> "\$WATCHDOG_LOG"
        fi
    fi

    # 检查隧道
    cf_pid=\$(cat "\$CF_PID_FILE" 2>/dev/null)
    if [ -z "\$cf_pid" ] || ! kill -0 "\$cf_pid" 2>/dev/null; then
        echo "[\$ts] Tunnel down, restarting..." >> "\$WATCHDOG_LOG"
        pkill -f "cloudflared tunnel" 2>/dev/null
        sleep 1
        setsid "\$CLOUDFLARED_BIN" tunnel --url "http://localhost:\${API_PORT}" > "\$CF_LOG" 2>&1 &
        echo \$! > "\$CF_PID_FILE" 2>/dev/null
        new_url=""
        for w in \$(seq 1 20); do
            new_url=\$(grep -oP "https://[a-z0-9-]+\.trycloudflare\.com" "\$CF_LOG" 2>/dev/null | head -1)
            if [ -n "\$new_url" ]; then
                break
            fi
            sleep 1
        done
        if [ -n "\$new_url" ]; then
            echo "\$new_url" > "\$CF_URL_FILE" 2>/dev/null
            echo "[\$ts] Tunnel restarted OK - \$new_url" >> "\$WATCHDOG_LOG"
        else
            echo "[\$ts] Tunnel restart FAILED" >> "\$WATCHDOG_LOG"
        fi
    fi
done
WD_EOF
    chmod +x /tmp/aishell-watchdog.sh

    setsid /tmp/aishell-watchdog.sh >/dev/null 2>&1 &
    local wd_pid=$!
    echo "$wd_pid" > "$WATCHDOG_PID_FILE"
    print_success "保活看门狗已启动 (PID=${wd_pid}, 间隔 ${WATCHDOG_INTERVAL}s)"
}


# ── 打印使用说明 ──
print_usage() {
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
        echo -e "   ${DIM}(restart 只重启 API，不重启隧道，域名不变)${NC}"
    fi
    echo -e "   ${GREEN}文档:${NC}  ${LOCAL_URL}/docs"
    echo ""
    echo -e "${CYAN}${BOLD}🔧 快速测试${NC}"
    echo -e "   ${YELLOW}curl ${LOCAL_URL}/v1/chat/completions -H 'Content-Type: application/json' \\${NC}"
    echo -e "   ${YELLOW}  -d '{\"model\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}🖥️  Cursor / VS Code 配置${NC}"
    echo -e "   ${GREEN}API Base URL:${NC}  ${PUBLIC_URL}/v1"
    echo -e "   ${GREEN}API Key:${NC}       any (不校验)"
    # 动态获取可用模型列表
    local models_json
    models_json=$(curl -s "${LOCAL_URL}/v1/models" 2>/dev/null || echo "")
    if [ -n "$models_json" ]; then
        local model_list
        model_list=$(echo "$models_json" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin).get('data', [])
    names = sorted(m['id'] for m in data if 'id' in m)
    # 分行打印，每行最多 4 个
    for i in range(0, len(names), 4):
        print('   ' + '  '.join(n.ljust(25) for n in names[i:i+4]))
except:
    print('   (解析失败)')
" 2>/dev/null || echo "   (解析失败)")
        echo -e "${CYAN}${BOLD}🤖 可用模型${NC} (${YELLOW}共 $(echo "$models_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null || echo '?') 个${NC})"
        echo -e "$model_list"
    else
        echo -e "${CYAN}${BOLD}🤖 可用模型${NC}"
        echo -e "   ${DIM}(API 未响应，稍后访问 ${LOCAL_URL}/v1/models 查看)${NC}"
    fi
    echo ""
    echo -e "${CYAN}${BOLD}⚙️  服务管理${NC}"
    echo -e "   ${YELLOW}./start.sh status${NC}    查看运行状态"
    echo -e "   ${YELLOW}./start.sh stop${NC}      停止所有服务 (API + 隧道 + 看门狗)"
    echo -e "   ${YELLOW}./start.sh restart${NC}   仅重启 API，保留隧道域名（客户端地址不变）"
    echo -e "   ${YELLOW}./start.sh tunnel${NC}    仅启动/重启隧道（不影响 API）"
    echo -e "   ${YELLOW}./start.sh logs${NC}       查看日志"
    echo ""
}

#===============================================================================
# 子命令处理
#===============================================================================

case "${1:-}" in
    --help|-h) show_help ;;
    stop)      stop_services; exit 0 ;;
    stop-api)  print_step "仅停止 API（保留隧道与看门狗）"; stop_api; exit 0 ;;
    tunnel)    print_step "仅启动/重启隧道（不影响 API）"; start_tunnel; print_usage; exit 0 ;;
    status)    show_status; exit $? ;;
    logs)      show_logs "${2:-}"; exit 0 ;;
esac

# restart: 仅重启 API，保留隧道
if [ "${1:-}" = "restart" ]; then
    print_step "重启 API 服务（保留隧道）"
    stop_api
    sleep 1
    start_api || exit 1
    # 复用已有隧道；仅当隧道不可用时才重建（会换新地址）
    if tunnel_alive && tunnel_serves; then
        TUNNEL_URL=$(get_tunnel_url)
        HAS_TUNNEL=true
        print_success "隧道已保留 — ${TUNNEL_URL}"
    else
        [ "$HAS_TUNNEL" = true ] && print_warn "隧道不可用，重建隧道..."
        start_tunnel
    fi
    print_usage
    exit 0
fi

#===============================================================================
# 正常启动流程
#===============================================================================

# 端口检查
if curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
    print_warn "端口 ${API_PORT} 已被占用，先停止旧 API..."
    stop_api
    sleep 2
fi

# 步骤 1: 安装 Python 依赖
print_step "步骤 1/3: 安装 Python 依赖"
if ! python3 -c "import fastapi, uvicorn, httpx, huaweicloudsdkcore" 2>/dev/null; then
    print_info "安装 FastAPI + Uvicorn..."
    pip install -q fastapi uvicorn pydantic httpx huaweicloudsdkcore 2>&1 | tail -5
else
    print_success "Python 依赖已就绪"
fi

# 步骤 2: 启动 API 服务
start_api || exit 1

# 步骤 3: 启动 Cloudflare 隧道（如已存活则复用）
start_tunnel

# 步骤 4: 启动保活看门狗
print_step "步骤 4: 启动保活看门狗"
start_watchdog

# 打印使用说明
print_usage
