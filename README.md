# aishell-glm52

OpenAI 兼容 API 代理服务，代理到华为云内置模型 (GLM-5.2 / openpangu-2.0-flash)。

## 特性

- ✅ 完整 OpenAI API 兼容 (`/v1/chat/completions`, `/v1/completions`, `/v1/models`, `/v1/embeddings`)
- ✅ 流式 SSE 严格对齐 OpenAI 格式 (chatcmpl- ID, role/content 分离, finish_reason, usage)
- ✅ 模型别名映射 (`gpt-4` → `glm-5.2`, `gpt-3.5-turbo` → `openpangu-2.0-flash` 等)
- ✅ 上游错误自动重试 (指数退避, 5xx/429/连接错误)
- ✅ **令牌桶限速器** — 控制发往上游的请求速率，避免 429
- ✅ **全局 429 熔断冷却** — 收到 429 后所有请求暂停，避免连续触发限速
- ✅ **429 专用重试** — 更多重试次数 + 指数退避 + 随机抖动
- ✅ 请求 ID 追踪, 结构化日志
- ✅ 连接池复用, 并发控制 (信号量)
- ✅ 可配置超时, 健康检查, Metrics 端点
- ✅ Cloudflare 隧道一键外网暴露
- ✅ **保活看门狗** — API/隧道挂掉自动恢复 (每 30s 检查)
- ✅ tool_calls / function calling 支持
- ✅ 所有额外参数透传 (stream_options, response_format, tools, ...)

## 快速开始

```bash
./start.sh              # 启动服务 + Cloudflare 隧道 + 保活看门狗
./start.sh status       # 查看状态 (API + 隧道 + 看门狗)
./start.sh restart      # 仅重启 API，保留隧道域名不变
./start.sh stop         # 停止所有服务
./start.sh logs         # 查看日志
./start.sh logs -f      # 实时跟踪日志
```

## 配置

通过 `config.json` 或环境变量配置：

| 参数 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| `api_port` | `API_PORT` | 8080 | 服务端口 |
| `connect_timeout` | `CONNECT_TIMEOUT` | 10 | 连接超时 (秒) |
| `read_timeout` | `READ_TIMEOUT` | 300 | 读取超时 (秒) |
| `max_retries` | `MAX_RETRIES` | 2 | 通用上游错误重试次数 |
| `max_concurrent` | `MAX_CONCURRENT` | 20 | 最大并发请求数 |
| `upstream_rate_limit` | — | 3.0 | 令牌桶速率 (请求/秒) |
| `upstream_rate_burst` | — | 2 | 令牌桶突发容量 |
| `retry_429_max` | — | 10 | 429 专用重试次数 |
| `retry_429_base` | — | 0.2 | 429 重试基础等待 (秒) |
| `NO_TUNNEL` | `NO_TUNNEL` | 0 | 设为 1 不启动隧道 |
| `NO_WATCHDOG` | `NO_WATCHDOG` | 0 | 设为 1 不启动看门狗 |

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

## 并发与限速

上游 TokenHub API 有 **4 req/s** 的速率限制。多客户端同时调用时，本服务通过以下机制避免 429：

```
客户端请求 → 令牌桶排队(3/s) → 全局冷却检查 → 信号量(≤20并发) → 上游
                ↑                                    ↓
                └──── 429 熔断冷却 1s ←── 429 响应 ──┘
```

| 场景 | 无限速 | 有限速 |
|------|--------|--------|
| 10 并发 | 80% 成功 | **100% 成功** |
| 20 并发 | ~50% 成功 | **75% 成功** |

> 20 并发时的失败是上游硬限速导致，无法完全避免。可通过降低 `upstream_rate_limit` 进一步减少失败率，代价是总耗时增加。

## 保活看门狗

`start.sh` 内置看门狗，每 30 秒检查一次：

- API 服务挂了 → 自动重启 (含 API Key 重新注入)
- Cloudflare 隧道挂了 → 自动重建并更新 URL
- 看门狗自身作为独立脚本运行，不受主脚本退出影响

## Cursor / VS Code 配置

```
API Base URL: https://<tunnel>.trycloudflare.com/v1
API Key:      any (不校验)
Model:        gpt-4 (或 glm-5.2, default)
```

## 参考

- [one-api](https://github.com/songquanpeng/one-api) - SSE headers, pass-through streaming
- [LiteLLM](https://github.com/BerriAI/litellm) - Model aliasing, retry logic
