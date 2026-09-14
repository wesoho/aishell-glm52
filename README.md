# OpenAI 兼容 API 服务

一个基于 FastAPI 的轻量级 API 服务，提供 **OpenAI 兼容接口**，可直接对接各类编程工具（Cursor、Copilot、OpenAI SDK 等）。通过 **Cloudflare Tunnel** 暴露到公网，受信任 HTTPS 地址，浏览器无安全提示。

## 功能特性

- ✅ **OpenAI 兼容**：`/v1/chat/completions`、`/v1/models` 接口格式完全兼容 OpenAI API
- ✅ **通用处理**：`/process` 接口接收任意数据，灵活处理
- ✅ **可插拔逻辑**：核心处理函数 `process_request()` 可自由替换
- ✅ **零配置启动**：无需 API Key 校验，开箱即用
- ✅ **自动文档**：内置 Swagger UI (`/docs`)
- ✅ **Cloudflare 隧道**：一键启动脚本自动安装 cloudflared 并建立公网隧道
- ✅ **GitHub 镜像加速**：通过 ghfast.top 等镜像代理下载，国内环境友好

## 一键启动

```bash
chmod +x start.sh && ./start.sh
```

脚本会自动完成：
1. 安装 Python 依赖（FastAPI + Uvicorn）
2. 启动 API 服务（端口 8080）
3. 通过 GitHub 镜像代理下载安装 cloudflared
4. 启动 Cloudflare 隧道，输出公网 HTTPS 地址

## 快速开始（手动）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
python main.py
# 服务运行在 http://0.0.0.0:8080
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

OpenAI 标准聊天接口，兼容所有支持 OpenAI API 的工具。

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

### 2. 模型列表 — `GET /v1/models`

```bash
curl http://localhost:8080/v1/models
```

### 3. 通用处理 — `POST /process`

接收任意数据，返回处理结果。

```bash
curl -X POST http://localhost:8080/process \
  -H "Content-Type: application/json" \
  -d '{"data": "任意内容", "options": {}}'
```

### 4. 健康检查 — `GET /health`

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

response = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "你的请求"}]
)
print(response.choices[0].message.content)
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
        content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content, ensure_ascii=False)
        parts.append(f"[{msg.role}] {content}")
    return "\n".join(parts)
```

修改后重启服务生效：
```bash
kill $(lsof -t -i:8080) && python main.py &
```

## 项目结构

```
api-server/
├── main.py            # 主程序（FastAPI 应用 + 处理逻辑）
├── start.sh           # 一键启动脚本（API + Cloudflare 隧道）
├── requirements.txt   # Python 依赖
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

## License

MIT
