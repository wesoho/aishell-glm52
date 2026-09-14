# aishell-glm52

OpenAI 兼容 API 代理服务，代理到华为云内置模型 (GLM-5.2 / openpangu-2.0-flash)。

## 特性

- ✅ 完整 OpenAI API 兼容 (`/v1/chat/completions`, `/v1/completions`, `/v1/models`, `/v1/embeddings`)
- ✅ 流式 SSE 严格对齐 OpenAI 格式 (chatcmpl- ID, role/content 分离, finish_reason, usage)
- ✅ 模型别名映射 (`gpt-4` → `glm-5.2`, `gpt-3.5-turbo` → `openpangu-2.0-flash` 等)
- ✅ 上游错误自动重试 (指数退避, 5xx/429/连接错误)
- ✅ 请求 ID 追踪, 结构化日志
- ✅ 连接池复用, 并发控制 (信号量)
- ✅ 可配置超时, 健康检查, Metrics 端点
- ✅ Cloudflare 隧道一键外网暴露
- ✅ tool_calls / function calling 支持
- ✅ 所有额外参数透传 (stream_options, response_format, tools, ...)

## 快速开始

```bash
./start.sh           # 启动服务 + Cloudflare 隧道
./start.sh status    # 查看状态
./start.sh stop      # 停止
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `API_PORT` | 8080 | 服务端口 |
| `CONNECT_TIMEOUT` | 10 | 连接超时 (秒) |
| `READ_TIMEOUT` | 300 | 读取超时 (秒) |
| `MAX_RETRIES` | 2 | 上游错误重试次数 |
| `MAX_CONCURRENT` | 20 | 最大并发请求数 |
| `NO_TUNNEL` | 0 | 设为 1 不启动隧道 |

## 模型别名

| 客户端模型名 | 实际上游模型 |
|------------|------------|
| `default` | openpangu-2.0-flash |
| `gpt-4` / `gpt-4o` / `gpt-4-turbo` | glm-5.2 |
| `gpt-3.5-turbo` / `gpt-3.5` | openpangu-2.0-flash |
| `claude-3-opus` / `claude-3-sonnet` | glm-5.2 |
| `glm-5.2` / `glm-5.1` / `openpangu-2.0-flash` | 原样传递 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI 聊天 (支持 stream) |
| POST | `/v1/completions` | OpenAI 文本补全 |
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/embeddings` | 向量嵌入 |
| GET | `/health` | 健康检查 |
| GET | `/metrics` | 运行指标 |
| POST | `/process` | 通用处理 |
| GET | `/docs` | Swagger 文档 |

## Cursor / VS Code 配置

```
API Base URL: https://<tunnel>.trycloudflare.com/v1
API Key:      any (不校验)
Model:        gpt-4 (或 glm-5.2, default)
```

## 参考

- [one-api](https://github.com/songquanpeng/one-api) - SSE headers, pass-through streaming
- [LiteLLM](https://github.com/BerriAI/litellm) - Model aliasing, retry logic
