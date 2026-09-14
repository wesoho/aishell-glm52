"""
OpenAI 兼容 API 代理服务 (v3.0)
代理到华为云内置模型 (GLM-5.2 / openpangu-2.0-flash)

特色:
- 完整 OpenAI API 兼容 (chat/completions, completions, models, embeddings)
- 流式 SSE 严格对齐 OpenAI 格式
- 模型别名映射 (gpt-4 → glm-5.2 等)
- 上游错误自动重试 (指数退避)
- 请求 ID 追踪, 结构化日志
- 连接池复用, 并发控制
- 可配置超时, 健康检查

参考: one-api (songquanpeng), LiteLLM (BerriAI)
"""

import os
import time
import json
import uuid
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────

UPSTREAM_BASE_URL = os.environ.get(
    "JOB_ENV_MODEL_BASE_URL",
    "https://tokenhub.developer.huaweicloud.com/v2",
)
UPSTREAM_API_KEY = os.environ.get("JOB_ENV_MODEL_API_KEY", "")
UPSTREAM_DEFAULT_MODEL = "openpangu-2.0-flash"
UPSTREAM_MODELS = ["openpangu-2.0-flash", "glm-5.2", "glm-5.1"]

# 模型别名: 客户端可用的友好名 → 上游实际模型
MODEL_ALIASES: Dict[str, str] = {
    "default": UPSTREAM_DEFAULT_MODEL,
    "gpt-4": "glm-5.2",
    "gpt-4o": "glm-5.2",
    "gpt-4-turbo": "glm-5.2",
    "gpt-3.5-turbo": "openpangu-2.0-flash",
    "gpt-3.5": "openpangu-2.0-flash",
    "claude-3-opus": "glm-5.2",
    "claude-3-sonnet": "glm-5.2",
}

ALL_MODELS = list(MODEL_ALIASES.keys()) + UPSTREAM_MODELS
DEFAULT_MAX_TOKENS = 4096

# 超时配置
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.environ.get("READ_TIMEOUT", "300"))
WRITE_TIMEOUT = float(os.environ.get("WRITE_TIMEOUT", "300"))

# 重试配置
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
RETRY_BACKOFF = 0.5

# 并发控制
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "20"))

# 如果环境变量没有 API_KEY，遍历 /proc 找
if not UPSTREAM_API_KEY:
    try:
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f"/proc/{pid_dir}/environ", "rb") as f:
                    env_data = f.read()
                for entry in env_data.split(b"\0"):
                    if entry.startswith(b"JOB_ENV_MODEL_API_KEY="):
                        UPSTREAM_API_KEY = entry.split(b"=", 1)[1].decode("utf-8")
                        break
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
            if UPSTREAM_API_KEY:
                break
    except Exception:
        pass

# ──────────────────────────────────────────────
# 日志配置
# ──────────────────────────────────────────────

LOG_FILE = os.environ.get("API_LOG_FILE", "/tmp/api-server.log")

logger = logging.getLogger("api-server")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
logger.addHandler(_ch)
_fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

req_logger = logging.getLogger("api-server.request")
req_logger.setLevel(logging.DEBUG)
_rfh = logging.FileHandler("/tmp/api-requests.log", mode="a", encoding="utf-8")
_rfh.setFormatter(_fmt)
req_logger.addHandler(_rfh)

API_PORT = int(os.environ.get("API_PORT", "8080"))

logger.info(f"Upstream: {UPSTREAM_BASE_URL}")
logger.info(f"Default model: {UPSTREAM_DEFAULT_MODEL}")
logger.info(f"API key configured: {'yes' if UPSTREAM_API_KEY else 'no'}")
logger.info(f"Max concurrent: {MAX_CONCURRENT}, Max retries: {MAX_RETRIES}")

# ──────────────────────────────────────────────
# 全局连接池 & 并发控制
# ──────────────────────────────────────────────

UPSTREAM_TIMEOUT = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT, write=WRITE_TIMEOUT)
_http_client: Optional[httpx.AsyncClient] = None
_semaphore: Optional[asyncio.Semaphore] = None

_metrics = {
    "total_requests": 0, "total_errors": 0, "stream_requests": 0,
    "total_latency_ms": 0.0, "start_time": time.time(),
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        logger.info("HTTP 连接池已关闭")


app = FastAPI(title="OpenAI Compatible API", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ──────────────────────────────────────────────
# 请求日志中间件
# ──────────────────────────────────────────────

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())[:8])
    request.state.request_id = request_id
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    method, path = request.method, request.url.path
    ua = request.headers.get("user-agent", "")
    auth = request.headers.get("authorization", "")

    req_logger.info(f"[{request_id}] → {method} {path} | IP={client_ip} | UA={ua[:60]} | Auth={'有' if auth else '无'}")

    try:
        response = await call_next(request)
    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        req_logger.error(f"[{request_id}] ✗ {method} {path} | 500 | {elapsed:.1f}ms | {type(e).__name__}: {e}")
        _metrics["total_errors"] += 1
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "internal_error", "param": None, "code": None}}, headers={"x-request-id": request_id})

    elapsed = (time.time() - start_time) * 1000
    status = response.status_code
    response.headers["x-request-id"] = request_id
    _metrics["total_requests"] += 1
    _metrics["total_latency_ms"] += elapsed
    if status >= 400:
        _metrics["total_errors"] += 1
        req_logger.warning(f"[{request_id}] ✗ {method} {path} | {status} | {elapsed:.1f}ms")
    else:
        req_logger.info(f"[{request_id}] ← {method} {path} | {status} | {elapsed:.1f}ms")
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
    """解析模型名: 别名 → 上游模型名"""
    if not model:
        return UPSTREAM_DEFAULT_MODEL
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    return model


def _validate_model(model: str) -> Optional[str]:
    if not model or model in MODEL_ALIASES or model in UPSTREAM_MODELS:
        return None
    return f"The model '{model}' does not exist. Available models: {', '.join(ALL_MODELS)}"


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
    """构建上游请求，转发所有参数"""
    payload = {
        "model": _resolve_model(body.get("model", "default")),
        "messages": body.get("messages", []),
        "stream": is_stream,
    }
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    if not max_tokens or max_tokens <= 0:
        max_tokens = DEFAULT_MAX_TOKENS
    payload["max_tokens"] = max_tokens
    # 转发所有额外参数 (tools, tool_choice, response_format, stream_options 等)
    SKIP_KEYS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}
    for key, value in body.items():
        if key not in SKIP_KEYS and value is not None:
            payload[key] = value
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
    """
    转换流式 SSE chunk，严格对齐 OpenAI 格式:
    - id 统一 chatcmpl- 前缀
    - 移除非标准字段 (service_tier, first_token_return_time, reasoning_content)
    - usage 只在最终 chunk 带
    - 每个 choice 补 finish_reason: null (非最终 chunk)
    - 首个 chunk 拆为 role chunk + content/tool_calls chunk
    - 确保 [DONE] 前有 finish_reason: stop 的 chunk
    """
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
            return f"data: {json.dumps(fin, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
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
        # 非流式: 带重试
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with get_semaphore():
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
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
    # 先列别名
    for alias in MODEL_ALIASES:
        data.append({"id": alias, "object": "model", "created": now, "owned_by": "system"})
    # 再列上游模型
    for model_id in UPSTREAM_MODELS:
        data.append({"id": model_id, "object": "model", "created": now, "owned_by": "huawei-cloud"})
    return {"object": "list", "data": data}


@app.post("/v1/embeddings")
async def embeddings(raw_request: Request):
    """转发 embeddings 请求到上游"""
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
        "status": "ok", "time": int(time.time()),
        "upstream": UPSTREAM_BASE_URL, "model": UPSTREAM_DEFAULT_MODEL,
        "api_key_configured": bool(UPSTREAM_API_KEY),
        "version": "3.0.0",
    }


@app.get("/v1/health")
async def health_v1():
    """OpenAI 兼容健康检查路径"""
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
        "upstream": UPSTREAM_BASE_URL,
    }


@app.get("/")
async def root():
    return {
        "service": "api-server", "version": "3.0.0", "mode": "proxy",
        "upstream": UPSTREAM_BASE_URL, "default_model": UPSTREAM_DEFAULT_MODEL,
        "available_models": ALL_MODELS,
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
