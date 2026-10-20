# aishell-glm52

OpenAI 兼容 API 代理服务，双上游代理到华为云模型 (TokenHub + Snap Access)。

## 特性

- ✅ 完整 OpenAI API 兼容 (`/v1/chat/completions`, `/v1/completions`, `/v1/models`, `/v1/embeddings`)
- ✅ **双上游路由** — TokenHub (Bearer token) + Snap Access (V4 HMAC 签名)，按模型名自动路由
- ✅ 流式 SSE 严格对齐 OpenAI 格式 (chatcmpl- ID, role/content 分离, finish_reason, usage)
- ✅ 兼容两种 SSE 格式 (TokenHub `data: {...}` 和 Snap Access `data:{...}`)
- ✅ 模型别名映射 (`gpt-4` → `glm-5.2`, `gpt-3.5-turbo` → `deepseek-v4-flash-0731` 等)
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

## 双上游架构

```
客户端请求
    │
    ├─ model = glm-5.2 / deepseek-v4-* → TokenHub (Bearer token)
    │                                      tokenhub.developer.huaweicloud.com/v2
    │
    └─ model = openpangu-* / glm-5.2-sft-harmony → Snap Access (V4 HMAC 签名)
                                           snap-access.cn-north-4.myhuaweicloud.com/api/v2
```

| 上游 | 认证方式 | 模型 |
|------|---------|------|
| TokenHub | Bearer token (`JOB_ENV_MODEL_API_KEY`) | glm-5.2, glm-5.1, deepseek-v4-flash-0731, deepseek-v4-pro-0813 |
| Snap Access | 华为云 V4 HMAC 签名 (AK/SK) | openpangu-2.0-flash, openpangu-2.0-pro, glm-5.2-sft-harmony |

Snap Access 使用华为云标准 V4 HMAC 签名认证（非 Bearer token），通过 `huaweicloudsdkcore` SDK 完成签名，每个请求动态生成 `Authorization: SDK-HMAC-SHA256 ...` 头。

## 快速安装（一条命令装好，全部模型可用）

```bash
git clone https://github.com/wesoho/aishell-glm52.git
cd aishell-glm52
bash install.sh          # 自动装依赖 + cloudflared + 注入 AK/SK + 启动
```

> 安装脚本会自动跳过已装部分（幂等），依赖安装与 cloudflared 下载并行执行。
> 华为云平台沙箱内运行时，AK/SK 由平台注入（`JOB_ENV_HW_*`），脚本自动映射。
> 默认端口 8080（可用环境变量 `API_PORT` 修改）。**重复运行 install.sh / start.sh 不会重启已在运行的隧道，外网地址保持不变。**

## 快速开始

```bash
./start.sh              # 启动全部 (API + Cloudflare 隧道 + 保活看门狗；隧道已运行则复用，地址不变)
./start.sh restart      # 仅重启 API —— 隧道不重启，外网地址不变，客户端无需改配置
./start.sh tunnel       # 仅启动/重启隧道（不影响 API；仅在隧道挂掉时才会换新地址）
./start.sh stop-api     # 仅停止 API（保留隧道与看门狗）
./start.sh stop         # 停止所有服务 (API + 隧道 + 看门狗)
./start.sh status       # 查看状态 (API + 隧道 + 看门狗)
./start.sh logs         # 查看日志
./start.sh logs -f      # 实时跟踪日志
```

> **隧道与服务分离**：`restart` / `stop-api` / 看门狗拉起 API 都不会重启 Cloudflare 隧道，免费隧道域名保持不变；只有隧道进程本身挂掉（或 `./start.sh stop` / 环境重建）才会重建并拿到新域名。

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
| `max_input_chars` | — | 300000 | prompt 最大输入字符数 (上游限制 307200) |

### API Key 自动校验（防止 401 apiKey解密失败）

启动 / 看门狗拉起 API 时，脚本会按优先级寻找 API Key 并**逐个向上游发最小请求校验**，无效的自动跳过（日志打印 `API Key 无效，跳过`），避免捡到其他环境凭证文件里的坏 Key 导致上游报 `401 apiKey解密失败`：

| 优先级 | 来源 |
|--------|------|
| 1 | 环境变量 `JOB_ENV_MODEL_API_KEY` |
| 2 | `/tmp/model_api_key.txt`（校验通过的缓存 / 手动覆盖文件） |
| 3 | `/root/job-envs/sandboxes/*/.dsh/.credentials.yaml` 等凭证文件 |
| 4 | `/proc/*/environ` 中其他进程携带的 key |

- 校验通过的 key 会写入 `/tmp/model_api_key.txt` 供看门狗与裸启动兜底。
- 手动指定 key：把有效 key 写入 `/tmp/model_api_key.txt`（单行无引号），然后 `./start.sh restart`。
- Key 是临时会话凭证（约 24h 过期），过期后出现 401 时更新 `/tmp/model_api_key.txt` 并 restart 即可。

### Snap Access 配置

在 `config.json` 的 `snap_access` 段配置：

```json
{
  "snap_access": {
    "base_url": "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2",
    "region": "cn-north-4",
    "models": ["openpangu-2.0-flash", "openpangu-2.0-pro", "glm-5.2-sft-harmony"]
  }
}
```

AK/SK 通过环境变量传入（`start.sh` 会自动从凭证文件注入）：

| 环境变量 | 说明 |
|---------|------|
| `HW_ACCESS_KEY` | 华为云 AK (Access Key) |
| `HW_SECRET_KEY` | 华为云 SK (Secret Key) |
| `HW_SECURITY_TOKEN` | 安全令牌 (临时凭证时需要，永久 AK/SK 可不设) |

> 未配置 AK/SK 时，Snap Access 模型不可用，TokenHub 模型正常工作。

## 模型列表

### TokenHub 模型 (Bearer token 认证)

| 客户端模型名 | 实际上游模型 |
|------------|------------|
| `default` | deepseek-v4-flash-0731 |
| `gpt-4` / `gpt-4o` / `gpt-4-turbo` | glm-5.2 |
| `gpt-3.5-turbo` / `gpt-3.5` | deepseek-v4-flash-0731 |
| `claude-3-opus` / `claude-3-sonnet` | glm-5.2 |
| `glm-5.2` / `glm-5.1` / `deepseek-v4-flash-0731` / `deepseek-v4-pro-0813` | 原样传递 |

### Snap Access 模型 (V4 HMAC 签名认证)

| 模型名 | 说明 |
|-------|------|
| `openpangu-2.0-flash` | 盘古 flash 模型 (快速) |
| `openpangu-2.0-pro` | 盘古 pro 模型 (高质量) |
| `glm-5.2-sft-harmony` | GLM-5.2 SFT 调和版 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI 聊天 (支持 stream) |
| POST | `/v1/completions` | OpenAI 文本补全 |
| GET | `/v1/models` | 模型列表 (含双上游所有模型) |
| POST | `/v1/embeddings` | 向量嵌入 |
| GET | `/health` | 健康检查 (含双上游状态) |
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

> 注意：令牌桶限速仅对 TokenHub 上游有意义。Snap Access 上游使用独立的 AK/SK 认证，限速策略可能不同。

## 保活看门狗

`start.sh` 内置看门狗，每 30 秒检查一次：

- API 服务挂了 → 自动重启 (含 API Key + AK/SK 重新注入)
- Cloudflare 隧道挂了 → 自动重建并更新 URL
- 看门狗自身作为独立脚本运行，不受主脚本退出影响

## Cursor / VS Code 配置

```
API Base URL: https://<tunnel>.trycloudflare.com/v1
API Key:      any (不校验)
Model:        gpt-4 (或 glm-5.2, default, openpangu-2.0-flash 等)
```

## 经验教训

> 部署过程中踩过的坑和解决方案，供后续参考。

### 1. STS 临时凭证无法脱离沙箱获取

API Key 是华为云 STS 临时凭证（~24h 过期），绑定沙箱会话，**无法脱离登录获取**。必须在华为云沙箱环境中运行，由 `JOB_ENV_MODEL_API_KEY` 自动注入。会话过期后 Key 失效返回 401，需重新登录刷新。

### 2. 沙箱重部署会杀死所有子进程

bwrap 容器重部署时，所有子进程（API 服务、Cloudflare 隧道）都会被杀死。解决方案：
- 看门狗写成**独立脚本文件**（`/tmp/aishell-watchdog.sh`），通过 `setsid` 启动为独立会话
- 看门狗不受主脚本的 `set -e` 影响，每 30s 轮询自动恢复挂掉的服务

### 3. `set -euo pipefail` 的传染性

bash 子脚本会继承父脚本的 `set -euo pipefail`，导致看门狗在任意命令返回非零时立即退出。解决方案：
- 看门狗脚本开头显式 `set +e` 关闭错误退出
- 使用独立脚本文件而非 `exec -a bash -c` 内嵌逻辑（后者还会丢失 PATH）

### 4. 上游硬限速不可绕过

TokenHub 上游 API 限制 **4 req/s**，这是服务端硬限制。令牌桶限速器只能**避免不必要的 429**（客户端侧排队），不能突破上游上限。

### 5. Snap Access V4 HMAC 签名

Snap Access 端点不接受 Bearer token，需要华为云标准 V4 HMAC 签名（`SDK-HMAC-SHA256`）。关键点：
- 使用 `huaweicloudsdkcore` SDK 的 `BasicCredentials` + `SdkRequest` + `Signer` 完成签名
- 签名时 body 必须与实际发送的 body **完全一致**（字节级），因此预序列化 body 后用 `content=` 发送，而非 `json=`
- `SdkRequest` 构造需要拆分 URL 为 `schema` / `host` / `resource_path` / `uri` 参数
- 临时凭证需额外设置 `security_token`，SDK 会自动添加 `X-Security-Token` 头

### 6. SSE 格式差异

TokenHub 和 Snap Access 的 SSE 格式有细微差异：
- TokenHub: `data: {...}` (data 后有空格)
- Snap Access: `data:{...}` (data 后无空格)

代理服务统一处理两种格式，`line[5:].strip()` 兼容两种写法。

### 7. 上游 prompt 字符数硬限制 307200

TokenHub 上游 API 限制 prompt 最大 **307200 字符**（注意是字符数不是 token 数），超出返回错误。解决方案：在发送上游前自动检测并截断，安全阈值 300000（可配置 `max_input_chars`）。

## 经验教训（续）

> ### 8. 沙箱内多个环境的凭证文件可能混入无效 Key（上游 401 apiKey解密失败）
> 华为云沙箱里每个环境（deepseek-harness / jiuwenswarm 等）都有自己的凭证文件；其中可能存着**加密信封格式**的 Key，直接发给 TokenHub 上游会被拒（`401 apiKey解密失败`）。v3.3 起 `start.sh` 与看门狗都会对候选 Key 逐个校验（发一个最小 chat 请求，200/429 视为有效），无效自动跳过再试下一个，并把有效的 Key 缓存到 `/tmp/model_api_key.txt`。不要再依赖“第一个捡到的 Key”。

## 参考

- [one-api](https://github.com/songquanpeng/one-api) - SSE headers, pass-through streaming
- [LiteLLM](https://github.com/BerriAI/litellm) - Model aliasing, retry logic
- [huaweicloudsdkcore](https://github.com/huaweicloud/huaweicloud-sdk-python-v3) - V4 HMAC 签名
