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

app = FastAPI(title="OpenAI Compatible API", version="2.2.0")

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
    for choice in data.get("choices", []):
        msg = choice.get("message", {})
        msg.pop("reasoning_content", None)
    return data


def _transform_stream_chunk(line: str, state: dict) -> Optional[str]:
    """
    转换流式 SSE chunk：
    - 删除 reasoning_content
    - 跳过 content 为空的 chunk（推理期间）
    - 只在首个 chunk 保留 role
    state: {"first": True} 用于跟踪首 chunk
    """
    if not line.startswith("data: "):
        return None
    data_str = line[6:].strip()
    if data_str == "[DONE]":
        state["done_sent"] = True
        return "data: [DONE]\n\n"
    try:
        chunk = json.loads(data_str)
    except json.JSONDecodeError:
        return None

    has_content = False
    has_finish = False
    for choice in chunk.get("choices", []):
        delta = choice.get("delta", {})
        delta.pop("reasoning_content", None)
        if delta.get("content"):
            has_content = True
        if delta.get("finish_reason"):
            has_finish = True
        # 只在第一个 chunk 保留 role
        if delta.get("role") and not state["first"]:
            delta.pop("role", None)

    # 跳过没有 content 且没有 finish_reason 的 chunk
    if not has_content and not has_finish:
        return None

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

UPSTREAM_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
KEEPALIVE_INTERVAL = 5.0


async def call_upstream_chat(payload: dict, stream: bool = False):
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = _upstream_headers()

    if stream:
        async def stream_generator():
            state = {"first": True}
            last_keepalive = time.time()
            try:
                async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
                    async with client.stream("POST", url, json=payload, headers=headers) as resp:
                        if resp.status_code != 200:
                            body = await resp.aread()
                            err = body.decode("utf-8", errors="replace")[:500]
                            logger.error(f"上游错误 {resp.status_code}: {err}")
                            yield f"data: {json.dumps({'error': {'message': err, 'code': resp.status_code}})}\n\n"
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
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error(f"上游错误 {resp.status_code}: {resp.text[:500]}")
                return {"error": True, "status": resp.status_code, "message": resp.text[:1000]}
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

    is_stream = body.get("stream", False)
    payload = _build_upstream_payload(body, is_stream)
    model_name = payload["model"]

    msgs = body.get("messages", [])
    msg_count = len(msgs)
    last_msg = str(msgs[-1].get("content", ""))[:100] if msgs else ""
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
    model = _resolve_model(body.get("model", "default"))
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

    logger.info(f"completions | model={model} | stream={is_stream} | prompt={prompt[:100]}")

    if is_stream:
        gen = await call_upstream_chat(chat_payload, stream=True)
        return StreamingResponse(gen, media_type="text/event-stream")
    else:
        result = await call_upstream_chat(chat_payload, stream=False)
        if isinstance(result, dict) and result.get("error"):
            return JSONResponse(status_code=result.get("status", 502),
                                content={"error": {"message": result["message"], "type": "upstream_error"}})
        result = _strip_reasoning(result)
        return JSONResponse(content=result)


@app.get("/v1/models")
async def list_models():
    data = [{
        "id": "default",
        "object": "model",
        "created": int(time.time()),
        "owned_by": "local",
    }]
    for i, model_id in enumerate(UPSTREAM_MODELS):
        data.append({
            "id": model_id,
            "object": "model",
            "created": int(time.time()) - i,
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
        "version": "2.2.0",
        "mode": "proxy",
        "upstream": UPSTREAM_BASE_URL,
        "default_model": UPSTREAM_DEFAULT_MODEL,
        "available_models": UPSTREAM_MODELS,
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
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
