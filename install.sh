#!/bin/bash
#===============================================================================
# aishell-glm52 快速安装脚本（并行加速版）
#   一次完成：源码 → Python 环境 → cloudflared → 注入 AK/SK → 启动（API+隧道）
#   特点：依赖安装与 cloudflared 下载并行执行，尽量快；已装部分自动跳过（幂等）。
#
# 用法：
#   bash install.sh                      # 仓库 clone 后直接跑，源码用本目录
#   bash install.sh /path/to/src         # 从指定源码目录拷贝
#
# 可配置环境变量：
#   PIP_MIRROR         pip 镜像（默认清华 tuna）
#   API_PORT           API 端口（默认 17180）
#   VENV_PYTHON_DIR    venv bin 目录（默认本目录 venv/bin）
#   CLOUDFLARED_BIN    cloudflared 路径（默认本目录 bin/cloudflared）
#   CF_LOG / CF_URL_FILE  隧道路由日志/URL 文件（默认本目录 cloudflared.log / cloudflared-url.txt）
#
# 启动完成后外网地址写入 $CF_URL_FILE，也可在启动输出中看到。
#
# 加速说明（2026-09 修订）：
#   GitHub 直连在国内经常超时，原逻辑每个源最多死等 150s 才切换，最坏 450s。
#   现改为：每个源先做 1MB Range 快速探测（10s 内判定），不通立即切下一个镜像；
#   官方源下载 25s 快速失败，镜像源 120s；整体最坏等待从分钟级降到秒级。
#===============================================================================
set -euo pipefail

RUNTIME="$(cd "$(dirname "$0")" && pwd)"
PIP_MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

echo "==> [1/5] 准备源码"
if [ -n "${1:-}" ] && [ -f "$1/main.py" ]; then
    cp "$1/main.py" "$1/config.json" "$1/requirements.txt" "$RUNTIME/"
elif [ -f "$RUNTIME/main.py" ]; then
    :
else
    echo "错误: 未找到源码。请传源码目录参数，或先在本目录放入 main.py/config.json/requirements.txt"
    exit 1
fi

# [2/5] Python 环境（venv + pip，尽量快）
echo "==> [2/5] Python 环境"
if [ ! -x "$RUNTIME/venv/bin/python" ]; then
    echo "    创建 venv ..."
    /usr/bin/python3 -m venv "$RUNTIME/venv"
fi
if ! "$RUNTIME/venv/bin/python" -c "import fastapi, uvicorn, httpx, huaweicloudsdkcore" 2>/dev/null; then
    echo "    安装依赖 (pip mirror: $PIP_MIRROR) ..."
    # --timeout/--retries 防止网络抖动时 pip 长时间静默卡死
    "$RUNTIME/venv/bin/pip" install -q --timeout 30 --retries 1 \
        --index-url "$PIP_MIRROR" -r "$RUNTIME/requirements.txt"
else
    echo "    依赖已就绪"
fi

# [3/5] cloudflared（与 [2] 并行：后台下载，最后 wait）
echo "==> [3/5] cloudflared（后台下载，与依赖安装并行）"
DL_PID=""
if [ ! -x "$RUNTIME/bin/cloudflared" ]; then
    mkdir -p "$RUNTIME/bin"
    (
        ARCH="$(uname -m)"
        case "$ARCH" in
            x86_64)  CFA="cloudflared-linux-amd64" ;;
            aarch64) CFA="cloudflared-linux-arm64" ;;
            *) echo "不支持的架构: $ARCH"; exit 1 ;;
        esac

        # 源列表：GitHub 官方优先，加速镜像兜底。
        # 每个源先 3s 快速探测，通了才下载；探测/下载失败立即切下一个，绝不死等。
        BASES=(
            "https://github.com/cloudflare/cloudflared/releases/latest/download"
            "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download"
            "https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download"
            "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download"
            "https://ghproxy.cc/https://github.com/cloudflare/cloudflared/releases/latest/download"
            "https://gh.ddlc.top/https://github.com/cloudflare/cloudflared/releases/latest/download"
            "https://github.moeyy.xyz/https://github.com/cloudflare/cloudflared/releases/latest/download"
        )
        for base in "${BASES[@]}"; do
            url="$base/$CFA"
            printf "    探测 %-20s " "${base#https://}"
            # Range 请求探测（跟随重定向，只取前 1MB）：比 HEAD 更能反映真实下载通道
            if ! curl -fsSL --connect-timeout 3 --max-time 10 -r 0-1048575 -o /dev/null "$url" 2>/dev/null; then
                echo "不通，切换镜像"
                continue
            fi
            echo "可用"
            # 官方源给 25s 快速失败（不行立即换镜像），镜像源给 120s
            TMO="120"
            case "$base" in
                https://github.com/*) TMO="25" ;;
            esac
            if curl -fSL --connect-timeout 5 --max-time "$TMO" -o "$RUNTIME/bin/cloudflared" "$url" 2>/dev/null \
               && [ "$(stat -c%s "$RUNTIME/bin/cloudflared" 2>/dev/null || echo 0)" -gt 10000000 ]; then
                chmod +x "$RUNTIME/bin/cloudflared"
                echo "    cloudflared 就绪 ($(stat -c%s "$RUNTIME/bin/cloudflared") bytes, 来自 ${base#https://})"
                exit 0
            fi
            rm -f "$RUNTIME/bin/cloudflared" 2>/dev/null || true
            echo "    下载失败/超时，切换镜像"
        done
        echo "    cloudflared 下载失败，请检查网络"; exit 1
    ) &
    DL_PID=$!
else
    echo "    cloudflared 已存在"
fi

# 等 cloudflared 下载（若在下载）
if [ -n "$DL_PID" ]; then
    echo "    等待 cloudflared 下载完成 ..."
    wait "$DL_PID" || exit 1
fi

# [4/5] 注入华为云 AK/SK（Snap Access 模型）
echo "==> [4/5] 注入华为云 AK/SK"
export HW_ACCESS_KEY="${HW_ACCESS_KEY:-${JOB_ENV_HW_ACCESS_KEY:-}}"
export HW_SECRET_KEY="${HW_SECRET_KEY:-${JOB_ENV_HW_SECRET_KEY:-}}"
export HW_SECURITY_TOKEN="${HW_SECURITY_TOKEN:-${JOB_ENV_HW_SECURITY_TOKEN:-}}"
export JOB_ENV_MODEL_API_KEY="${JOB_ENV_MODEL_API_KEY:-}"
if [ -n "$HW_ACCESS_KEY" ] && [ -n "$HW_SECRET_KEY" ]; then
    echo "    AK/SK 就绪 (AK ${#HW_ACCESS_KEY} 位)"
else
    echo "    警告: 未找到 AK/SK，Snap Access 模型(盘古)不可用，TokenHub 模型正常"
fi

# [5/5] 启动服务
echo "==> [5/5] 启动服务"
cd "$RUNTIME"
export PATH="$RUNTIME/venv/bin:$PATH"
export API_PORT="${API_PORT:-17180}"
export CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$RUNTIME/bin/cloudflared}"
export CF_LOG="${CF_LOG:-$RUNTIME/cloudflared.log}"
export CF_URL_FILE="${CF_URL_FILE:-$RUNTIME/cloudflared-url.txt}"
bash "$RUNTIME/start.sh"

echo ""
CF_URL=$(cat "${CF_URL_FILE:-}" 2>/dev/null || true)
if [ -n "$CF_URL" ]; then
    echo "外网地址: $CF_URL"
else
    echo "外网地址: (隧道仍未建立，稍后查看 $CF_URL_FILE / $CF_LOG)"
fi