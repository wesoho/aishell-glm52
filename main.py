"""
OpenAI 兼容 API 服务 — 代理到内置模型 (GLM-5.2 / openpangu-2.0-flash)
- POST /v1/chat/completions  (OpenAI 标准聊天接口，支持 stream)
- POST /v1/completions       (OpenAI 文本补全接口)
- GET  /v1/models            (模型列表)
- GET  /health               (健康检查)
- POST /process              (通用处理接口)

请求直接转发到华为云内置模型端点，返回真实 AI 回复。
流式响应中剥离 reasoning_content，只传 content；推理期间发 keepalive 防超时。
"""

import os
import time
import json
import logging
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

# ──────────────────────────────────────────────
# 上游模型配置
# ──────────────────────────────────────────────

UPSTREAM_BASE_URL = os.environ.get(
    "JOB_ENV_MODEL_BASE_URL",
    "https://tokenhub.developer.huaweicloud.com/v2",
)
UPSTREAM_API_KEY = os.environ.get("JOB_ENV_MODEL_API_KEY", "")
UPSTREAM_DEFAULT_MODEL = "openpangu-2.0-flash"
UPSTREAM_MODELS = ["openpangu-2.0-flash", "glm-5.2", "glm-5.1"]
ALL_MODELS = ["default"] + UPSTREAM_MODELS
DEFAULT_MAX_TOKENS = 4096

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
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(console_handler)
file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)

req_logger = logging.getLogger("api-server.request")
req_logger.setLevel(logging.DEBUG)
req_file_handler = logging.FileHandler("/tmp/api-requests.log", mode="a", encoding="utf-8")
req_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
req_logger.addHandler(req_file_handler)

API_PORT = int(os.environ.get("API_PORT", "8080"))

logger.info(f"Upstream: {UPSTREAM_BASE_URL}")
logger.info(f"Default model: {UPSTREAM_DEFAULT_MODEL}")
logger.info(f"API key configured: {'yes' if UPSTREAM_API_KEY else 'no'}")

# ──────────────────────────────────────────────
# 全局连接池 (复用 httpx.AsyncClient)
# ──────────────────────────────────────────────

UPSTREAM_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
KEEPALIVE_INTERVAL = 5.0

_http_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=UPSTREAM_TIMEOUT,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
        )
    return _http_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化，关闭时清理连接池"""
    yield
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        logger.info("HTTP 连接池已关闭")


app = FastAPI(title="OpenAI Compatible API", version="2.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# 请求日志中间件
# ──────────────────────────────────────────────

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    method = request.method
    path = request.url.path
    user_agent = request.headers.get("user-agent", "")
    auth = request.headers.get("authorization", "")

    req_logger.info(
        f"→ {method} {path} | IP={client_ip} | "
        f"UA={user_agent[:80]} | Auth={'有' if auth else '无'}"
    )

    try:
        response = await call_next(request)
    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        req_logger.error(f"✗ {method} {path} | 500 | {elapsed:.1f}ms | {type(e).__name__}: {e}")
        logger.exception(f"异常: {method} {path}")
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "internal_error"}})

    elapsed = (time.time() - start_time) * 1000
    status = response.status_code
    if status >= 400:
        req_logger.warning(f"✗ {method} {path} | {status} | {elapsed:.1f}ms")
    else:
        req_logger.info(f"← {method} {path} | {status} | {elapsed:.1f}ms")
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
    if not model or model == "default":
        return UPSTREAM_DEFAULT_MODEL
    return model


def _validate_model(model: str) -> Optional[str]:
    """校验模型名，返回错误消息或 None"""
    if not model or model == "default":
        return None
    if model not in UPSTREAM_MODELS:
        return f"Model '{model}' not found. Available models: {', '.join(ALL_MODELS)}"
    return None


def _validate_messages(messages: list) -> Optional[str]:
    """校验 messages，返回错误消息或 None"""
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
    max_tokens = body.get("max_tokens")
    if max_tokens is None or max_tokens <= 0:
        max_tokens = DEFAULT_MAX_TOKENS
    payload["max_tokens"] = max_tokens

    for key in ("temperature", "top_p",
                "frequency_penalty", "presence_penalty", "n", "user",
                "stop", "seed"):
        if key in body and body[key] is not None:
            payload[key] = body[key]
    return payload


def _strip_reasoning(data: dict) -> dict:
    """剥离非标准字段，保持 OpenAI 兼容"""
    for choice in data.get("choices", []):
        msg = choice.get("message", {})
        msg.pop("reasoning_content", None)
    data.pop("service_tier", None)
    return data


def _parse_upstream_error(text: str) -> str:
    """解析上游错误，提取可读消息"""
    try:
        err = json.loads(text)
        # 嵌套 error 对象
        if "error" in err and isinstance(err["error"], dict):
            return err["error"].get("message", text)
        if "error_msg" in err:
            return err["error_msg"]
        return text
    except (json.JSONDecodeError, TypeError):
        return text


def _make_chunk_id(state: dict) -> str:
    """生成 OpenAI 风格的 chatcmpl- ID"""
    raw_id = state.get("chunk_id", "")
    if raw_id.startswith("chatcmpl-"):
        return raw_id
    return f"chatcmpl-{raw_id}" if raw_id else f"chatcmpl-{int(time.time()*1000)}"


def _transform_stream_chunk(line: str, state: dict) -> Optional[str]:
    """
    转换流式 SSE chunk，严格对齐 OpenAI 格式：
    - id 统一为 chatcmpl- 前缀
    - 移除 usage: null（只在最终 chunk 带 usage）
    - 移除 service_tier、first_token_return_time 等非标准字段
    - 每个 choice 补 finish_reason: null（非最终 chunk）
    - 首个 content chunk 拆为 role chunk + content chunk
    - 确保 [DONE] 前有 finish_reason: stop 的 chunk
    """
    if not line.startswith("data: "):
        return None
    data_str = line[6:].strip()
    if data_str == "[DONE]":
        state["done_sent"] = True
        if not state.get("finish_sent"):
            cid = _make_chunk_id(state)
            fin_chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": state.get("created", int(time.time())),
                "model": state.get("model", ""),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            if state.get("usage"):
                fin_chunk["usage"] = state["usage"]
            return f"data: {json.dumps(fin_chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
        return "data: [DONE]\n\n"
    try:
        chunk = json.loads(data_str)
    except json.JSONDecodeError:
        return None

    # 剥离非标准字段
    chunk.pop("service_tier", None)
    chunk.pop("first_token_return_time", None)
    chunk.pop("usage", None)  # 移除 usage: null，只在最终 chunk 带

    # 记录元信息
    if "id" in chunk:
        state["chunk_id"] = chunk["id"]
    if "model" in chunk:
        state["model"] = chunk["model"]
    if "created" in chunk:
        state["created"] = chunk["created"]

    # 统一 id 格式
    chunk["id"] = _make_chunk_id(state)

    has_content = False
    has_finish = False
    for choice in chunk.get("choices", []):
        delta = choice.get("delta", {})
        delta.pop("reasoning_content", None)
        if delta.get("content"):
            has_content = True
        if delta.get("finish_reason"):
            has_finish = True
            state["finish_sent"] = True
        else:
            # 非最终 chunk 补 finish_reason: null
            choice["finish_reason"] = None
        # 非首 chunk 剥离 role（只在第一个 chunk 带 role）
        if delta.get("role") and not state["first"]:
            delta.pop("role", None)

    if not has_content and not has_finish:
        return None

    # 首个 chunk: 如果同时有 role 和 content，拆成两个 chunk
    if state["first"] and has_content:
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("role") and delta.get("content"):
                content = delta["content"]
                # 先发 role-only chunk
                role_chunk = json.loads(json.dumps(chunk))
                role_chunk["choices"][0]["delta"] = {"role": "assistant"}
                role_chunk["choices"][0]["finish_reason"] = None
                # 再发 content chunk
                delta.pop("role", None)
                state["first"] = False
                return f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\ndata: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    state["first"] = False
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ──────────────────────────────────────────────
# 上游调用
# ──────────────────────────────────────────────

def _upstream_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTREAM_API_KEY}",
    }


async def call_upstream_chat(payload: dict, stream: bool = False):
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = _upstream_headers()
    client = await get_client()

    if stream:
        async def stream_generator():
            state = {
                "first": True, "done_sent": False, "finish_sent": False,
                "usage": None, "chunk_id": "", "model": "", "created": 0,
            }
            last_keepalive = time.time()
            try:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        err_text = body.decode("utf-8", errors="replace")[:500]
                        err_msg = _parse_upstream_error(err_text)
                        logger.error(f"上游错误 {resp.status_code}: {err_msg}")
                        yield f"data: {json.dumps({'error': {'message': err_msg, 'code': resp.status_code}})}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        transformed = _transform_stream_chunk(line, state)
                        if transformed:
                            yield transformed
                            last_keepalive = time.time()
                        else:
                            now = time.time()
                            if now - last_keepalive > KEEPALIVE_INTERVAL:
                                yield ": keepalive\n\n"
                                last_keepalive = now
                    if not state.get("done_sent"):
                        # 补 finish_reason chunk
                        if not state.get("finish_sent"):
                            fin_chunk = {
                                "id": state.get("chunk_id", ""),
                                "object": "chat.completion.chunk",
                                "created": state.get("created", int(time.time())),
                                "model": state.get("model", ""),
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }
                            if state.get("usage"):
                                fin_chunk["usage"] = state["usage"]
                            yield f"data: {json.dumps(fin_chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
            except httpx.ConnectError as e:
                logger.error(f"上游连接失败: {e}")
                yield f"data: {json.dumps({'error': {'message': f'连接上游失败: {e}'}})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.exception(f"流式请求异常: {e}")
                yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
                yield "data: [DONE]\n\n"
        return stream_generator()
    else:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            err_msg = _parse_upstream_error(resp.text[:1000])
            logger.error(f"上游错误 {resp.status_code}: {err_msg}")
            return {"error": True, "status": resp.status_code, "message": err_msg}
        return resp.json()

# ──────────────────────────────────────────────
# API 接口
# ──────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(raw_request: Request):
    try:
        body = await raw_request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": {"message": f"Invalid JSON: {e}"}})

    # 模型校验
    model_input = body.get("model", "default")
    model_err = _validate_model(model_input)
    if model_err:
        return JSONResponse(status_code=404, content={"error": {"message": model_err, "type": "invalid_request_error"}})

    # messages 校验
    messages = body.get("messages")
    msg_err = _validate_messages(messages)
    if msg_err:
        return JSONResponse(status_code=400, content={"error": {"message": msg_err, "type": "invalid_request_error"}})

    is_stream = body.get("stream", False)
    payload = _build_upstream_payload(body, is_stream)
    model_name = payload["model"]

    msg_count = len(messages)
    last_msg = str(messages[-1].get("content", ""))[:100] if messages else ""
    req_logger.info(f"  model={model_name} stream={is_stream} msgs={msg_count} last='{last_msg}'")
    logger.info(f"chat | model={model_name} | stream={is_stream} | msgs={msg_count}")

    if is_stream:
        gen = await call_upstream_chat(payload, stream=True)
        return StreamingResponse(gen, media_type="text/event-stream")
    else:
        result = await call_upstream_chat(payload, stream=False)
        if isinstance(result, dict) and result.get("error"):
            return JSONResponse(status_code=result.get("status", 502),
                                content={"error": {"message": result["message"], "type": "upstream_error"}})
        result = _strip_reasoning(result)
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.info(f"chat done | model={model_name} | content_len={len(content)} | content={content[:200]}")
        return JSONResponse(content=result)


@app.post("/v1/completions")
async def completions(raw_request: Request):
    try:
        body = await raw_request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": {"message": f"Invalid JSON: {e}"}})

    prompt = body.get("prompt", "")
    model_input = body.get("model", "default")
    model_err = _validate_model(model_input)
    if model_err:
        return JSONResponse(status_code=404, content={"error": {"message": model_err, "type": "invalid_request_error"}})

    model = _resolve_model(model_input)
    is_stream = body.get("stream", False)

    chat_payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": is_stream,
        "max_tokens": body.get("max_tokens") or DEFAULT_MAX_TOKENS,
    }
    for key in ("temperature", "top_p"):
        if key in body and body[key] is not None:
            chat_payload[key] = body[key]

    logger.info(f"completions | model={model} | stream={is_stream} | prompt={str(prompt)[:100]}")

    if is_stream:
        gen = await call_upstream_chat(chat_payload, stream=True)
        return StreamingResponse(gen, media_type="text/event-stream")
    else:
        result = await call_upstream_chat(chat_payload, stream=False)
        if isinstance(result, dict) and result.get("error"):
            return JSONResponse(status_code=result.get("status", 502),
                                content={"error": {"message": result["message"], "type": "upstream_error"}})
        result = _strip_reasoning(result)

        # 转换为 OpenAI completions 格式 (text 而非 message)
        choices = result.get("choices", [])
        text_content = ""
        finish_reason = "stop"
        if choices:
            text_content = choices[0].get("message", {}).get("content", "")
            finish_reason = choices[0].get("finish_reason", "stop")

        completion_response = {
            "id": result.get("id", f"cmpl-{int(time.time())}"),
            "object": "text_completion",
            "created": result.get("created", int(time.time())),
            "model": model,
            "choices": [{
                "text": text_content,
                "index": 0,
                "finish_reason": finish_reason,
            }],
            "usage": result.get("usage", {}),
        }
        return JSONResponse(content=completion_response)

@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    data = [{
        "id": "default",
        "object": "model",
        "created": now,
        "owned_by": "local",
    }]
    for model_id in UPSTREAM_MODELS:
        data.append({
            "id": model_id,
            "object": "model",
            "created": now,
            "owned_by": "huawei-cloud",
        })
    return {"object": "list", "data": data}


@app.post("/process")
async def process(raw_request: Request):
    try:
        body = await raw_request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": {"message": f"Invalid JSON: {e}"}})

    content = str(body.get("data", ""))
    payload = {
        "model": UPSTREAM_DEFAULT_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
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


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "time": int(time.time()),
        "upstream": UPSTREAM_BASE_URL,
        "model": UPSTREAM_DEFAULT_MODEL,
        "api_key_configured": bool(UPSTREAM_API_KEY),
    }


@app.get("/")
async def root():
    return {
        "service": "api-server",
        "version": "2.4.0",
        "mode": "proxy",
        "upstream": UPSTREAM_BASE_URL,
        "default_model": UPSTREAM_DEFAULT_MODEL,
        "available_models": ALL_MODELS,
        "endpoints": {
            "chat": "/v1/chat/completions",
            "completions": "/v1/completions",
            "models": "/v1/models",
            "process": "/process",
            "health": "/health",
            "docs": "/docs",
        },
    }


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting on port {API_PORT}, upstream={UPSTREAM_BASE_URL}, model={UPSTREAM_DEFAULT_MODEL}")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=API_PORT,
        log_level="info",
        access_log=False,
        timeout_keep_alive=30,
    )
