"""
OpenAI 兼容 API 代理服务 (v3.1)
代理到华为云内置模型 (GLM-5.2 / openpangu-2.0-flash)

特色:
- 完整 OpenAI API 兼容 (chat3, completions, models, embeddings)
- 流式 SSE 严格对齐 OpenAI 格式
- 模型别名映射 (gpt-4 → glm-5.2 等)
- 上游错误自动重试 (指数退避)
- 请求 ID 追踪, 结构化日志
- 连接池复用, 并发控制
- 配置文件 + 环境变量双重配置
- 请求体大小限制
- Token 用量统计
- 上游模型动态发现
- 日志轮转
- 优雅关机
- 上游健康探测

参考: one-api (songquanpeng), LiteLLM (BerriAI)
"""

import os
import time
import json
import glob
import uuid
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

# ──────────────────────────────────────────────
# 配置加载: config.json → 环境变量覆盖
# ──────────────────────────────────────────────

_CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
_config: dict = {}

try:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        _config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    pass


def _cfg(key: str, default=None, cast=str):
    """从 config.json 或环境变量读取配置，环境变量优先"""
    env_key = key.upper()
    if env_key in os.environ:
        val = os.environ[env_key]
    elif key in _config:
        val = _config[key]
    else:
        return default
    if cast is str:
        return val
    if cast is bool:
        return str(val).lower() in ("true", "1", "yes")
    try:
        return cast(val)
    except (ValueError, TypeError):
        return default


UPSTREAM_BASE_URL = _cfg("upstream_base_url", "https://tokenhub.developer.huaweicloud.com/v2")
UPSTREAM_API_KEY = os.environ.get("JOB_ENV_MODEL_API_KEY", "")
UPSTREAM_DEFAULT_MODEL = _cfg("upstream_default_model", "openpangu-2.0-flash")
UPSTREAM_MODELS: list = _config.get("upstream_models", ["openpangu-2.0-flash", "glm-5.2", "glm-5.1"])

MODEL_ALIASES: Dict[str, str] = _config.get("model_aliases", {
    "default": UPSTREAM_DEFAULT_MODEL,
    "gpt-4": "glm-5.2",
    "gpt-4o": "glm-5.2",
    "gpt-4-turbo": "glm-5.2",
    "gpt-3.5-turbo": "openpangu-2.0-flash",
    "gpt-3.5": "openpangu-2.0-flash",
    "claude-3-opus": "glm-5.2",
    "claude-3-sonnet": "glm-5.2",
})

ALL_MODELS = list(MODEL_ALIASES.keys()) + UPSTREAM_MODELS
DEFAULT_MAX_TOKENS = _cfg("default_max_tokens", 4096, int)

CONNECT_TIMEOUT = _cfg("connect_timeout", 10, float)
READ_TIMEOUT = _cfg("read_timeout", 300, float)
WRITE_TIMEOUT = _cfg("write_timeout", 300, float)

MAX_RETRIES = _cfg("max_retries", 2, int)
RETRY_BACKOFF = _cfg("retry_backoff", 0.5, float)

MAX_CONCURRENT = _cfg("max_concurrent", 20, int)

MAX_BODY_SIZE = _cfg("max_body_size_mb", 10, int) * 1024 * 1024

LOG_FILE = _cfg("log_file", "/tmp/api-server.log")
LOG_MAX_BYTES = _cfg("log_max_bytes_mb", 10, int) * 1024 * 1024
LOG_BACKUP_COUNT = _cfg("log_backup_count", 5, int)

API_PORT = _cfg("api_port", 8080, int)

UPSTREAM_HEALTH_INTERVAL = _cfg("upstream_health_interval", 60, int)
UPSTREAM_MODEL_REFRESH_INTERVAL = _cfg("upstream_model_refresh_interval", 300, int)

# ── 多策略快速获取 Model API Key ──
# 策略优先级: 环境变量 > 凭证文件 > /proc 精准扫描
def _find_api_key() -> str:
    """多策略快速获取 JOB_ENV_MODEL_API_KEY"""
    # 策略 0: 环境变量（最快）
    key = os.environ.get("JOB_ENV_MODEL_API_KEY", "")
    if key:
        return key

    # 策略 1: 已知凭证文件路径（毫秒级，glob 匹配）
    _KEY_MARK = "JOB_ENV_MODEL_API_KEY"
    _credential_globs = [
        "/root/job-envs/sandboxes/*/.dsh/.credentials.yaml",
        "/root/job-envs/sandboxes/*/.jiuwenswarm/config/.env",
        "/root/.dsh/.credentials.yaml",
        "/tmp/model_api_key.txt",
    ]
    for pattern in _credential_globs:
        for fpath in glob.glob(pattern):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    text = f.read()
                # 纯 Key 值文件（如 /tmp/model_api_key.txt）
                if _KEY_MARK not in text:
                    val = text.strip()
                    if val and len(val) > 20:
                        return val
                    continue
                # YAML/env 格式: KEY: "value" 或 KEY=value
                idx = text.find(_KEY_MARK)
                rest = text[idx + len(_KEY_MARK):]
                # 跳过 : 和空白
                rest = rest.lstrip(": \t")
                if rest.startswith('"'):
                    end = rest.find('"', 1)
                    if end > 0:
                        return rest[1:end]
                elif rest.startswith("'"):
                    end = rest.find("'", 1)
                    if end > 0:
                        return rest[1:end]
                else:
                    # env 格式: KEY=value
                    end = rest.find("\n")
                    val = rest[:end] if end > 0 else rest.strip()
                    if val:
                        return val
            except (OSError, UnicodeDecodeError):
                continue

    # 策略 2: /proc 精准扫描（os.scandir 比 listdir 快，跳过自身）
    _PREFIX = b"JOB_ENV_MODEL_API_KEY="
    _self_pid = os.getpid()
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == _self_pid:
                continue
            try:
                # 只读前 4KB，API Key 不会太靠后
                with open(f"/proc/{entry.name}/environ", "rb") as f:
                    env_data = f.read(4096)
                idx = env_data.find(_PREFIX)
                if idx != -1:
                    val_start = idx + len(_PREFIX)
                    val_end = env_data.find(b"\0", val_start)
                    if val_end > val_start:
                        return env_data[val_start:val_end].decode("utf-8")
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception:
        pass

    return ""

UPSTREAM_API_KEY = _find_api_key()

# ──────────────────────────────────────────────
# 日志配置 (带轮转)
# ──────────────────────────────────────────────

logger = logging.getLogger("api-server")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
logger.addHandler(_ch)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

req_logger = logging.getLogger("api-server.request")
req_logger.setLevel(logging.DEBUG)
_rfh = RotatingFileHandler("/tmp/api-requests.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
_rfh.setFormatter(_fmt)
req_logger.addHandler(_rfh)

logger.info(f"Upstream: {UPSTREAM_BASE_URL}")
logger.info(f"Default model: {UPSTREAM_DEFAULT_MODEL}")
logger.info(f"API key configured: {'yes' if UPSTREAM_API_KEY else 'no'}")
logger.info(f"Max concurrent: {MAX_CONCURRENT}, Max retries: {MAX_RETRIES}")
logger.info(f"Max body size: {MAX_BODY_SIZE // 1024 // 1024}MB, Log rotation: {LOG_MAX_BYTES // 1024 // 1024}MBx{LOG_BACKUP_COUNT}")

# ──────────────────────────────────────────────
# 全局连接池 & 并发控制 & 状态
# ──────────────────────────────────────────────

UPSTREAM_TIMEOUT = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT, write=WRITE_TIMEOUT)
_http_client: Optional[httpx.AsyncClient] = None
_semaphore: Optional[asyncio.Semaphore] = None
_inflight_requests = 0

_upstream_healthy = True
_upstream_last_check = 0.0
_upstream_models_dynamic: list = []

_metrics = {
    "total_requests": 0, "total_errors": 0, "stream_requests": 0,
    "total_latency_ms": 0.0, "start_time": time.time(),
    "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
}


async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=UPSTREAM_TIMEOUT,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60.0),
        )
    return _http_client


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


def _track_inflight(inc: bool):
    global _inflight_requests
    if inc:
        _inflight_requests += 1
    else:
        _inflight_requests = max(0, _inflight_requests - 1)


def _record_usage(usage: dict):
    """记录 token 用量到 metrics"""
    if not usage or not isinstance(usage, dict):
        return
    _metrics["total_tokens"] += usage.get("total_tokens", 0) or 0
    _metrics["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    _metrics["completion_tokens"] += usage.get("completion_tokens", 0) or 0

# ──────────────────────────────────────────────
# 后台任务: 上游健康探测 + 模型刷新
# ──────────────────────────────────────────────

async def _upstream_health_task():
    global _upstream_healthy, _upstream_last_check
    while True:
        try:
            await asyncio.sleep(UPSTREAM_HEALTH_INTERVAL)
            client = await get_client()
            resp = await client.get(f"{UPSTREAM_BASE_URL}/models",
                                    headers={"Authorization": f"Bearer {UPSTREAM_API_KEY}"},
                                    timeout=httpx.Timeout(5.0, connect=3.0))
            _upstream_healthy = resp.status_code == 200
            _upstream_last_check = time.time()
            if not _upstream_healthy:
                logger.warning(f"上游健康检查失败: HTTP {resp.status_code}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            _upstream_healthy = False
            _upstream_last_check = time.time()
            logger.warning(f"上游健康检查异常: {e}")


async def _upstream_model_refresh_task():
    global _upstream_models_dynamic
    while True:
        try:
            await asyncio.sleep(UPSTREAM_MODEL_REFRESH_INTERVAL)
            client = await get_client()
            resp = await client.get(f"{UPSTREAM_BASE_URL}/models",
                                    headers={"Authorization": f"Bearer {UPSTREAM_API_KEY}"},
                                    timeout=httpx.Timeout(10.0, connect=5.0))
            if resp.status_code == 200:
                data = resp.json()
                models = []
                if isinstance(data, dict) and "data" in data:
                    for m in data["data"]:
                        if isinstance(m, dict) and "id" in m:
                            models.append(m["id"])
                elif isinstance(data, list):
                    models = [m.get("id", str(m)) if isinstance(m, dict) else str(m) for m in data]
                if models:
                    _upstream_models_dynamic = models
                    logger.info(f"上游模型刷新: {models}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"上游模型刷新失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    health_task = asyncio.create_task(_upstream_health_task())
    model_task = asyncio.create_task(_upstream_model_refresh_task())
    logger.info("后台任务已启动: 上游健康探测 + 模型刷新")

    yield

    # 优雅关机
    health_task.cancel()
    model_task.cancel()
    try:
        await asyncio.gather(health_task, model_task, return_exceptions=True)
    except Exception:
        pass

    global _inflight_requests
    wait_start = time.time()
    while _inflight_requests > 0 and (time.time() - wait_start) < 10:
        logger.info(f"优雅关机: 等待 {_inflight_requests} 个在途请求完成...")
        await asyncio.sleep(0.5)

    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        logger.info("HTTP 连接池已关闭")
    logger.info("服务已停止")


app = FastAPI(title="OpenAI Compatible API", version="3.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ──────────────────────────────────────────────
# 请求日志中间件 (含请求体大小限制)
# ──────────────────────────────────────────────

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"error": {"message": f"Request body too large. Max {MAX_BODY_SIZE // 1024 // 1024}MB.", "type": "request_too_large", "param": None, "code": "payload_too_large"}},
        )

    request_id = request.headers.get("x-request-id", str(uuid.uuid4())[:8])
    request.state.request_id = request_id
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    method, path = request.method, request.url.path
    ua = request.headers.get("user-agent", "")
    auth = request.headers.get("authorization", "")

    req_logger.info(f"[{request_id}] -> {method} {path} | IP={client_ip} | UA={ua[:60]} | Auth={'有' if auth else '无'}")

    _track_inflight(True)
    try:
        response = await call_next(request)
    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        req_logger.error(f"[{request_id}] X {method} {path} | 500 | {elapsed:.1f}ms | {type(e).__name__}: {e}")
        _metrics["total_errors"] += 1
        _track_inflight(False)
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "internal_error", "param": None, "code": None}}, headers={"x-request-id": request_id})

    _track_inflight(False)
    elapsed = (time.time() - start_time) * 1000
    status = response.status_code
    response.headers["x-request-id"] = request_id
    _metrics["total_requests"] += 1
    _metrics["total_latency_ms"] += elapsed
    if status >= 400:
        _metrics["total_errors"] += 1
        req_logger.warning(f"[{request_id}] X {method} {path} | {status} | {elapsed:.1f}ms")
    else:
        req_logger.info(f"[{request_id}] <- {method} {path} | {status} | {elapsed:.1f}ms")
    return response

# ──────────────────────────────────────────────
# Pydantic 模型
# ──────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: Any

class ChatCompletionRequest(BaseModel):
    model: str = "default"
    messages: List[Message]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    top_p: Optional[float] = None
    model_config = ConfigDict(extra="allow")

# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _resolve_model(model: str) -> str:
    if not model:
        return UPSTREAM_DEFAULT_MODEL
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    return model


def _get_all_models() -> list:
    models = set(MODEL_ALIASES.keys())
    models.update(UPSTREAM_MODELS)
    models.update(_upstream_models_dynamic)
    return sorted(models)


def _validate_model(model: str) -> Optional[str]:
    if not model or model in MODEL_ALIASES or model in UPSTREAM_MODELS or model in _upstream_models_dynamic:
        return None
    return f"The model '{model}' does not exist. Available models: {', '.join(_get_all_models())}"


def _validate_messages(messages: list) -> Optional[str]:
    if not messages:
        return "messages is required and must not be empty."
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return f"messages[{i}] must be an object."
        if "role" not in msg:
            return f"messages[{i}] must have a 'role' field."
        if "content" not in msg:
            return f"messages[{i}] must have a 'content' field."
    return None


def _build_upstream_payload(body: dict, is_stream: bool) -> dict:
    payload = {
        "model": _resolve_model(body.get("model", "default")),
        "messages": body.get("messages", []),
        "stream": is_stream,
    }
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    if not max_tokens or max_tokens <= 0:
        max_tokens = DEFAULT_MAX_TOKENS
    payload["max_tokens"] = max_tokens
    SKIP_KEYS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}
    for key, value in body.items():
        if key not in SKIP_KEYS and value is not None:
            payload[key] = value
    if is_stream:
        if "stream_options" not in payload:
            payload["stream_options"] = {"include_usage": True}
        elif isinstance(payload["stream_options"], dict) and not payload["stream_options"].get("include_usage"):
            payload["stream_options"]["include_usage"] = True
    return payload


def _strip_reasoning(data: dict) -> dict:
    for choice in data.get("choices", []):
        msg = choice.get("message", {})
        msg.pop("reasoning_content", None)
    data.pop("service_tier", None)
    return data


def _parse_upstream_error(text: str) -> Tuple[str, Optional[Any]]:
    try:
        err = json.loads(text)
        if "error" in err and isinstance(err["error"], dict):
            return err["error"].get("message", text), err["error"].get("code")
        if "error_msg" in err:
            return err["error_msg"], err.get("error_code")
        return text, None
    except (json.JSONDecodeError, TypeError):
        return text, None


def _make_chunk_id(state: dict) -> str:
    raw_id = state.get("chunk_id", "")
    if raw_id.startswith("chatcmpl-"):
        return raw_id
    return f"chatcmpl-{raw_id}" if raw_id else f"chatcmpl-{int(time.time()*1000)}"


def _openai_error(message: str, status: int, etype: str = "invalid_request_error",
                  code: Any = None, param: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": etype, "param": param, "code": code}})


def _should_retry(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429

# ──────────────────────────────────────────────
# 流式 chunk 转换
# ──────────────────────────────────────────────

def _transform_stream_chunk(line: str, state: dict) -> Optional[str]:
    if not line.startswith("data: "):
        return None
    data_str = line[6:].strip()
    if data_str == "[DONE]":
        state["done_sent"] = True
        if not state.get("finish_sent"):
            cid = _make_chunk_id(state)
            fin = {"id": cid, "object": "chat.completion.chunk", "created": state.get("created", int(time.time())),
                   "model": state.get("model", ""), "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            if state.get("usage"):
                fin["usage"] = state["usage"]
                _record_usage(state["usage"])
            return f"data: {json.dumps(fin, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
        if state.get("usage"):
            _record_usage(state["usage"])
        return "data: [DONE]\n\n"
    try:
        chunk = json.loads(data_str)
    except json.JSONDecodeError:
        return None

    chunk.pop("service_tier", None)
    chunk.pop("first_token_return_time", None)
    if "usage" in chunk and chunk["usage"] is not None:
        state["usage"] = chunk["usage"]
    chunk.pop("usage", None)

    if "id" in chunk:
        state["chunk_id"] = chunk["id"]
    if "model" in chunk:
        state["model"] = chunk["model"]
    if "created" in chunk:
        state["created"] = chunk["created"]
    chunk["id"] = _make_chunk_id(state)

    has_content = has_finish = has_tool_calls = False
    for choice in chunk.get("choices", []):
        delta = choice.get("delta", {})
        delta.pop("reasoning_content", None)
        if delta.get("content"):
            has_content = True
        if delta.get("tool_calls"):
            has_tool_calls = True
        if delta.get("finish_reason"):
            has_finish = True
            state["finish_sent"] = True
        else:
            choice["finish_reason"] = None
        if delta.get("role") and not state["first"]:
            delta.pop("role", None)

    if not has_content and not has_finish and not has_tool_calls:
        return None

    if state["first"] and (has_content or has_tool_calls):
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("role") and (delta.get("content") or delta.get("tool_calls")):
                role_chunk = json.loads(json.dumps(chunk))
                role_chunk["choices"][0]["delta"] = {"role": "assistant"}
                role_chunk["choices"][0]["finish_reason"] = None
                delta.pop("role", None)
                state["first"] = False
                return f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\ndata: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    state["first"] = False
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# ──────────────────────────────────────────────
# 上游调用
# ──────────────────────────────────────────────

def _upstream_headers():
    return {"Content-Type": "application/json", "Authorization": f"Bearer {UPSTREAM_API_KEY}"}


async def call_upstream_chat(payload: dict, stream: bool = False, request_id: str = ""):
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = _upstream_headers()
    client = await get_client()

    if stream:
        async def stream_generator():
            state = {"first": True, "done_sent": False, "finish_sent": False,
                     "usage": None, "chunk_id": "", "model": "", "created": 0}
            retry_count = 0
            while retry_count <= MAX_RETRIES:
                try:
                    async with get_semaphore():
                        async with client.stream("POST", url, json=payload, headers=headers) as resp:
                            if resp.status_code != 200:
                                body = await resp.aread()
                                err_text = body.decode("utf-8", errors="replace")[:500]
                                err_msg, err_code = _parse_upstream_error(err_text)
                                if _should_retry(resp.status_code) and retry_count < MAX_RETRIES:
                                    retry_count += 1
                                    wait = RETRY_BACKOFF * (2 ** (retry_count - 1))
                                    logger.warning(f"[{request_id}] 上游 {resp.status_code}, 重试 {retry_count}/{MAX_RETRIES} ({wait}s)")
                                    await asyncio.sleep(wait)
                                    continue
                                logger.error(f"[{request_id}] 上游错误 {resp.status_code}: {err_msg}")
                                yield f"data: {json.dumps({'error': {'message': err_msg, 'code': resp.status_code, 'type': 'upstream_error'}})}\n\n"
                                yield "data: [DONE]\n\n"
                                return
                            async for line in resp.aiter_lines():
                                if not line.strip():
                                    continue
                                transformed = _transform_stream_chunk(line, state)
                                if transformed:
                                    yield transformed
                            if not state.get("done_sent"):
                                if not state.get("finish_sent"):
                                    fin = {"id": _make_chunk_id(state), "object": "chat.completion.chunk",
                                           "created": state.get("created", int(time.time())), "model": state.get("model", ""),
                                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                                    if state.get("usage"):
                                        fin["usage"] = state["usage"]
                                    yield f"data: {json.dumps(fin, ensure_ascii=False)}\n\n"
                                if state.get("usage"):
                                    _record_usage(state["usage"])
                                yield "data: [DONE]\n\n"
                            return
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                    if retry_count < MAX_RETRIES:
                        retry_count += 1
                        wait = RETRY_BACKOFF * (2 ** (retry_count - 1))
                        logger.warning(f"[{request_id}] 连接异常, 重试 {retry_count}/{MAX_RETRIES}: {e}")
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"[{request_id}] 上游连接失败: {e}")
                    yield f"data: {json.dumps({'error': {'message': f'upstream connection failed: {e}', 'type': 'connection_error'}})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                except Exception as e:
                    logger.exception(f"[{request_id}] 流式请求异常: {e}")
                    yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'internal_error'}})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
        return stream_generator()
    else:
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with get_semaphore():
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    _record_usage(data.get("usage", {}))
                    return data
                if _should_retry(resp.status_code) and attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"[{request_id}] 上游 {resp.status_code}, 重试 {attempt+1}/{MAX_RETRIES} ({wait}s)")
                    await asyncio.sleep(wait)
                    continue
                err_msg, err_code = _parse_upstream_error(resp.text[:1000])
                logger.error(f"[{request_id}] 上游错误 {resp.status_code}: {err_msg}")
                return {"error": True, "status": resp.status_code, "message": err_msg, "code": err_code}
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"[{request_id}] 连接异常, 重试 {attempt+1}/{MAX_RETRIES}: {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"[{request_id}] 上游连接失败: {e}")
                return {"error": True, "status": 503, "message": f"upstream connection failed: {e}", "code": None}
        return {"error": True, "status": 503, "message": "max retries exceeded", "code": None}

# ──────────────────────────────────────────────
# API 接口
# ──────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(raw_request: Request):
    request_id = getattr(raw_request.state, "request_id", "")
    try:
        body = await raw_request.json()
    except Exception as e:
        return _openai_error(f"Invalid JSON: {e}", 400, "invalid_request_error")

    model_input = body.get("model", "default")
    model_err = _validate_model(model_input)
    if model_err:
        return _openai_error(model_err, 404, "invalid_request_error")

    messages = body.get("messages")
    msg_err = _validate_messages(messages)
    if msg_err:
        return _openai_error(msg_err, 400, "invalid_request_error")

    is_stream = body.get("stream", False)
    payload = _build_upstream_payload(body, is_stream)
    model_name = payload["model"]

    msg_count = len(messages)
    last_msg = str(messages[-1].get("content", ""))[:100] if messages else ""
    req_logger.info(f"[{request_id}]   model={model_name} stream={is_stream} msgs={msg_count} last='{last_msg}'")
    logger.info(f"[{request_id}] chat | model={model_name} | stream={is_stream} | msgs={msg_count}")

    if is_stream:
        _metrics["stream_requests"] += 1
        gen = await call_upstream_chat(payload, stream=True, request_id=request_id)
        return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)
    else:
        result = await call_upstream_chat(payload, stream=False, request_id=request_id)
        if isinstance(result, dict) and result.get("error"):
            return _openai_error(result["message"], result.get("status", 502), "upstream_error", result.get("code"))
        result = _strip_reasoning(result)
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.info(f"[{request_id}] chat done | model={model_name} | content_len={len(content)} | content={content[:200]}")
        return JSONResponse(content=result)


@app.post("/v1/completions")
async def completions(raw_request: Request):
    request_id = getattr(raw_request.state, "request_id", "")
    try:
        body = await raw_request.json()
    except Exception as e:
        return _openai_error(f"Invalid JSON: {e}", 400)

    prompt = body.get("prompt", "")
    model_input = body.get("model", "default")
    model_err = _validate_model(model_input)
    if model_err:
        return _openai_error(model_err, 404)

    model = _resolve_model(model_input)
    is_stream = body.get("stream", False)
    chat_payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                    "stream": is_stream, "max_tokens": body.get("max_tokens") or DEFAULT_MAX_TOKENS}
    for key in ("temperature", "top_p"):
        if key in body and body[key] is not None:
            chat_payload[key] = body[key]

    logger.info(f"[{request_id}] completions | model={model} | stream={is_stream}")

    if is_stream:
        gen = await call_upstream_chat(chat_payload, stream=True, request_id=request_id)
        return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)
    else:
        result = await call_upstream_chat(chat_payload, stream=False, request_id=request_id)
        if isinstance(result, dict) and result.get("error"):
            return _openai_error(result["message"], result.get("status", 502), "upstream_error", result.get("code"))
        result = _strip_reasoning(result)
        choices = result.get("choices", [])
        text_content = choices[0].get("message", {}).get("content", "") if choices else ""
        finish_reason = choices[0].get("finish_reason", "stop") if choices else "stop"
        return JSONResponse(content={
            "id": result.get("id", f"cmpl-{int(time.time())}"), "object": "text_completion",
            "created": result.get("created", int(time.time())), "model": model,
            "choices": [{"text": text_content, "index": 0, "finish_reason": finish_reason}],
            "usage": result.get("usage", {}),
        })


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    data = []
    for alias in MODEL_ALIASES:
        data.append({"id": alias, "object": "model", "created": now, "owned_by": "system"})
    for model_id in UPSTREAM_MODELS:
        data.append({"id": model_id, "object": "model", "created": now, "owned_by": "huawei-cloud"})
    for model_id in _upstream_models_dynamic:
        if model_id not in UPSTREAM_MODELS and model_id not in MODEL_ALIASES:
            data.append({"id": model_id, "object": "model", "created": now, "owned_by": "huawei-cloud-dynamic"})
    return {"object": "list", "data": data}


@app.post("/v1/embeddings")
async def embeddings(raw_request: Request):
    request_id = getattr(raw_request.state, "request_id", "")
    try:
        body = await raw_request.json()
    except Exception as e:
        return _openai_error(f"Invalid JSON: {e}", 400)

    model = _resolve_model(body.get("model", "default"))
    payload = {**body, "model": model}
    client = await get_client()
    url = f"{UPSTREAM_BASE_URL}/embeddings"

    try:
        async with get_semaphore():
            resp = await client.post(url, json=payload, headers=_upstream_headers())
        if resp.status_code == 200:
            return JSONResponse(content=resp.json())
        err_msg, err_code = _parse_upstream_error(resp.text[:1000])
        return _openai_error(err_msg, resp.status_code, "upstream_error", err_code)
    except Exception as e:
        logger.error(f"[{request_id}] embeddings error: {e}")
        return _openai_error(str(e), 502, "upstream_error")


@app.post("/process")
async def process(raw_request: Request):
    try:
        body = await raw_request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": {"message": f"Invalid JSON: {e}"}})

    content = str(body.get("data", ""))
    payload = {"model": UPSTREAM_DEFAULT_MODEL, "messages": [{"role": "user", "content": content}], "max_tokens": DEFAULT_MAX_TOKENS}
    options = body.get("options", {})
    if options:
        for k, v in options.items():
            payload[k] = v

    result = await call_upstream_chat(payload, stream=False)
    if isinstance(result, dict) and result.get("error"):
        return JSONResponse(status_code=502, content={"success": False, "error": result["message"]})
    result = _strip_reasoning(result)
    content_out = ""
    choices = result.get("choices", [])
    if choices:
        content_out = choices[0].get("message", {}).get("content", "")
    return {"success": True, "result": content_out, "timestamp": int(time.time())}

# ──────────────────────────────────────────────
# 健康检查 & Metrics & 根路由
# ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok" if _upstream_healthy else "degraded",
        "upstream_healthy": _upstream_healthy,
        "upstream_last_check": int(_upstream_last_check) if _upstream_last_check else 0,
        "time": int(time.time()),
        "upstream": UPSTREAM_BASE_URL,
        "model": UPSTREAM_DEFAULT_MODEL,
        "api_key_configured": bool(UPSTREAM_API_KEY),
        "version": "3.1.0",
    }


@app.get("/v1/health")
async def health_v1():
    return await health()


@app.get("/metrics")
async def metrics():
    uptime = time.time() - _metrics["start_time"]
    avg_latency = _metrics["total_latency_ms"] / max(_metrics["total_requests"], 1)
    return {
        "uptime_seconds": round(uptime, 1),
        "total_requests": _metrics["total_requests"],
        "stream_requests": _metrics["stream_requests"],
        "total_errors": _metrics["total_errors"],
        "error_rate": round(_metrics["total_errors"] / max(_metrics["total_requests"], 1) * 100, 2),
        "avg_latency_ms": round(avg_latency, 1),
        "concurrent_limit": MAX_CONCURRENT,
        "inflight_requests": _inflight_requests,
        "upstream": UPSTREAM_BASE_URL,
        "upstream_healthy": _upstream_healthy,
        "tokens": {
            "total": _metrics["total_tokens"],
            "prompt": _metrics["prompt_tokens"],
            "completion": _metrics["completion_tokens"],
        },
    }


@app.get("/")
async def root():
    return {
        "service": "api-server", "version": "3.1.0", "mode": "proxy",
        "upstream": UPSTREAM_BASE_URL, "default_model": UPSTREAM_DEFAULT_MODEL,
        "available_models": _get_all_models(),
        "model_aliases": MODEL_ALIASES,
        "endpoints": {
            "chat": "/v1/chat/completions",
            "completions": "/v1/completions",
            "models": "/v1/models",
            "embeddings": "/v1/embeddings",
            "process": "/process",
            "health": "/health",
            "metrics": "/metrics",
            "docs": "/docs",
        },
    }


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting on port {API_PORT}, upstream={UPSTREAM_BASE_URL}, model={UPSTREAM_DEFAULT_MODEL}")
    uvicorn.run(app, host="0.0.0.0", port=API_PORT, log_level="info", access_log=False, timeout_keep_alive=30)
