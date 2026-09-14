# OpenAI 兼容 API 服务

基于 FastAPI 的轻量级 API 服务，提供 **OpenAI 兼容接口**，可直接对接 Cursor、Copilot、OpenAI SDK 等工具。通过 **Cloudflare Tunnel** 暴露公网，受信任 HTTPS，浏览器无安全提示。

## 功能特性

- ✅ **OpenAI 兼容**：`/v1/chat/completions`、`/v1/completions`、`/v1/models` 完全兼容 OpenAI API
- ✅ **流式响应**：支持 `stream: true`，兼容 SSE 客户端
- ✅ **CORS 支持**：浏览器前端可直接调用
- ✅ **通用处理**：`/process` 接口接收任意数据 + options
- ✅ **可插拔逻辑**：核心处理函数 `process_request()` 可自由替换
- ✅ **零配置启动**：无需 API Key 校验，开箱即用
- ✅ **自动文档**：内置 Swagger UI (`/docs`)
- ✅ **Cloudflare 隧道**：一键脚本自动安装 cloudflared 并建立公网隧道
- ✅ **GitHub 镜像加速**：通过 ghfast.top 等镜像代理下载，国内环境友好
- ✅ **环境变量配置**：`API_PORT` 自定义端口，`NO_TUNNEL` 跳过隧道
- ✅ **人性化输出**：启动后自动打印访问地址、测试命令、代码示例、工具配置，开箱即用

## 一键启动

```bash
chmod +x start.sh && ./start.sh
```

脚本会自动完成：
1. 安装 Python 依赖（FastAPI + Uvicorn）
2. 启动 API 服务（端口 8080）
3. 通过 GitHub 镜像代理下载安装 cloudflared
4. 启动 Cloudflare 隧道，输出公网 HTTPS 地址
5. **打印完整使用说明**（访问地址、测试命令、代码示例、工具配置）

### 启动后输出示例

脚本启动完成后会直接打印使用指南，用户无需翻文档即可上手：

```
✅  服务启动成功，可以开始使用了！

📍 访问地址
   本地:  http://localhost:8080
   外网:  https://xxx.trycloudflare.com
   文档:  http://localhost:8080/docs

🔧 快速测试 (复制即可运行)
   curl http://localhost:8080/health
   curl http://localhost:8080/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"default","messages":[{"role":"user","content":"你好"}]}'

🐍 Python 调用 (OpenAI SDK)
   from openai import OpenAI
   client = OpenAI(base_url="https://xxx.trycloudflare.com/v1", api_key="any")
   ...

🖥️  Cursor / VS Code 配置
   API Base URL:  https://xxx.trycloudflare.com/v1
   API Key:       any
   Model:          default

📋 接口一览 / ⚙️ 服务管理 / 💡 自定义处理逻辑
```

### 脚本子命令

```bash
./start.sh              # 启动服务
./start.sh stop         # 停止所有服务
./start.sh restart      # 重启服务
./start.sh status       # 查看运行状态
./start.sh --help       # 显示帮助
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_PORT` | `8080` | API 服务端口 |
| `NO_TUNNEL` | `0` | 设为 `1` 则不启动 Cloudflare 隧道 |

```bash
# 自定义端口
API_PORT=9000 ./start.sh

# 仅启动 API，不启动隧道
NO_TUNNEL=1 ./start.sh
```

## 快速开始（手动）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
python main.py
# 服务运行在 http://0.0.0.0:8080
# 自定义端口: API_PORT=9000 python main.py
```

### 启动 Cloudflare 隧道

```bash
# 安装 cloudflared（通过 GitHub 镜像代理）
ARCH=$(uname -m)
case "${ARCH}" in
    x86_64)  CFA_FILE="cloudflared-linux-amd64" ;;
    aarch64) CFA_FILE="cloudflared-linux-arm64" ;;
esac
curl -fSL -o /usr/local/bin/cloudflared "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/${CFA_FILE}"
chmod +x /usr/local/bin/cloudflared

# 启动隧道
cloudflared tunnel --url http://localhost:8080
# 终端会输出 https://xxx.trycloudflare.com 地址
```

## API 接口说明

### 1. 聊天补全 — `POST /v1/chat/completions`

OpenAI 标准聊天接口，支持流式响应。

**请求：**
```json
{
  "model": "default",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "temperature": 1.0,
  "max_tokens": null,
  "stream": false
}
```

**响应：**
```json
{
  "id": "chatcmpl-xxxx",
  "object": "chat.completion",
  "created": 1789360123,
  "model": "default",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "处理结果"},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 2,
    "completion_tokens": 4,
    "total_tokens": 6
  }
}
```

**流式响应** (`stream: true`)：返回 SSE 格式 `text/event-stream`，兼容 OpenAI SDK 流式调用。

### 2. 文本补全 — `POST /v1/completions`

```bash
curl -X POST http://localhost:8080/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"default","prompt":"你好"}'
```

### 3. 模型列表 — `GET /v1/models`

```bash
curl http://localhost:8080/v1/models
```

### 4. 通用处理 — `POST /process`

接收任意数据 + options，返回处理结果。

```bash
curl -X POST http://localhost:8080/process \
  -H "Content-Type: application/json" \
  -d '{"data": "任意内容", "options": {"key": "value"}}'
```

### 5. 健康检查 — `GET /health`

```bash
curl http://localhost:8080/health
# {"status": "ok", "time": 1789360123}
```

## 编程工具对接

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="any"  # 不校验，任意值即可
)

# 普通调用
response = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "你的请求"}]
)
print(response.choices[0].message.content)

# 流式调用
for chunk in client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "你的请求"}],
    stream=True
):
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### 外网调用（通过 Cloudflare 隧道）

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://your-tunnel-url.trycloudflare.com/v1",
    api_key="any"
)

response = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)
```

### cURL

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"default","messages":[{"role":"user","content":"你好"}]}'
```

### Cursor / VS Code 配置

在工具设置中将 API Base URL 指向服务地址：
```
Base URL: http://localhost:8080/v1
API Key: any
Model: default
```

## 自定义处理逻辑

修改 `main.py` 中的 `process_request()` 函数：

```python
def process_request(messages: List[Message], **kwargs) -> str:
    """
    自定义你的处理逻辑。
    kwargs 可包含: model, temperature, max_tokens, top_p, options 等

    - 调用 AI 模型
    - 数据格式转换
    - 业务计算
    - 数据库查询
    - ...
    """
    # 示例：调用外部 AI API
    # import requests
    # resp = requests.post("https://api.example.com/v1/chat", json={...})
    # return resp.json()["result"]

    # 当前：回显消息
    parts = []
    for msg in messages:
        content = _content_to_str(msg.content)
        parts.append(f"[{msg.role}] {content}")
    return "\n".join(parts)
```

修改后重启服务生效：
```bash
./start.sh restart
```

## 项目结构

```
aishell-glm52/
├── main.py            # 主程序（FastAPI 应用 + 处理逻辑）
├── start.sh           # 一键启动脚本（API + Cloudflare 隧道 + 使用说明）
├── requirements.txt   # Python 依赖（版本锁定）
└── README.md          # 说明文档
```

## 技术栈

- **FastAPI** — 高性能异步 Web 框架
- **Uvicorn** — ASGI 服务器
- **Pydantic** — 数据验证与序列化
- **Cloudflare Tunnel** — 免费公网 HTTPS 隧道，无需注册

## Cloudflare 隧道说明

- 使用 `cloudflared tunnel --url` 创建快速隧道，无需 Cloudflare 账号
- 隧道地址格式：`https://xxx.trycloudflare.com`
- 受信任的 HTTPS 证书，浏览器无安全提示
- 快速隧道 URL 每次启动会变化
- 如需固定 URL，请配置 Cloudflare 命名隧道（需账号）

## 更新日志

### v2.1
- 🎨 **人性化启动输出**：启动后自动打印访问地址、快速测试命令、Python 代码示例、Cursor/VS Code 配置、接口一览、服务管理命令，用户无需翻文档即可上手
- 🐛 **修复隧道等待退出**：`set -euo pipefail` 下 `grep` 无匹配时脚本误退出，添加 `|| true` 保护
- ⏱️ **隧道等待超时**：从 20 秒增加到 30 秒，适应网络较慢的环境

### v2.0
- 添加 Cloudflare Tunnel 一键启动支持
- 添加 GitHub 镜像代理下载
- 添加脚本子命令 (stop/restart/status)
- 添加流式响应支持
- 添加 CORS 支持

## License

MIT
