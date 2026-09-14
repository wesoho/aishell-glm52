"""
OpenAI 兼容 API 服务
- POST /v1/chat/completions  (OpenAI 标准聊天接口，支持 stream)
- POST /v1/completions       (OpenAI 文本补全接口)
- GET  /v1/models            (模型列表)
- GET  /health               (健康检查)
- POST /process              (通用处理接口)

处理逻辑在 process_request() 中，可自由修改。
配置: 环境变量 API_PORT (默认 8080)
"""

import os
import time
import uuid
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api-server")

API_PORT = int(os.environ.get("API_PORT", "8080"))

app = FastAPI(title="OpenAI Compatible API", version="1.1.0")

# CORS — 允许浏览器前端直接调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# 请求/响应模型 (OpenAI 兼容)
# ──────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: Any

class ChatCompletionRequest(BaseModel):
    model: str = "default"
    messages: List[Message]
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    top_p: Optional[float] = 1.0
    model_config = ConfigDict(extra="allow")

class CompletionRequest(BaseModel):
    model: str = "default"
    prompt: str
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    model_config = ConfigDict(extra="allow")

class ProcessRequest(BaseModel):
    data: Any
    options: Optional[Dict[str, Any]] = None

# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _content_to_str(content: Any) -> str:
    """把消息内容统一转成字符串，用于 token 计算和处理。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return json.dumps(content, ensure_ascii=False)


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（英文约 1 token/4字符，中文约 1 token/2字符）。"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return (ascii_chars // 4) + (non_ascii // 2) + 1


# ──────────────────────────────────────────────
# 核心处理逻辑 — 在这里自定义你的处理方式
# ──────────────────────────────────────────────

def process_request(messages: List[Message], **kwargs) -> str:
    """
    处理用户请求，返回结果文本。
    当前实现：拼接所有消息内容并回显。
    替换此函数即可实现任意处理逻辑。

    kwargs 可包含: model, temperature, max_tokens, top_p, options 等
    """
    parts = []
    for msg in messages:
        content = _content_to_str(msg.content)
        parts.append(f"[{msg.role}] {content}")
    return "\n".join(parts)


# ──────────────────────────────────────────────
# OpenAI 兼容接口
# ──────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    extra = {k: v for k, v in req.model_dump().items() if k != "messages"}

    try:
        result_text = process_request(req.messages, **extra)
    except Exception as e:
        logger.exception("process_request failed")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_error"}},
        )

    prompt_tokens = sum(_estimate_tokens(_content_to_str(m.content)) for m in req.messages)
    completion_tokens = _estimate_tokens(result_text)
    created = int(time.time())
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    # 流式响应
    if req.stream:
        async def generate():
            chunks = [
                {"id": resp_id, "object": "chat.completion.chunk", "created": created,
                 "model": req.model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
                {"id": resp_id, "object": "chat.completion.chunk", "created": created,
                 "model": req.model, "choices": [{"index": 0, "delta": {"content": result_text}, "finish_reason": None}]},
                {"id": resp_id, "object": "chat.completion.chunk", "created": created,
                 "model": req.model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")

    return JSONResponse(content={
        "id": resp_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result_text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


@app.post("/v1/completions")
async def completions(req: CompletionRequest):
    """OpenAI 文本补全接口。"""
    msgs = [Message(role="user", content=req.prompt)]
    extra = {k: v for k, v in req.model_dump().items() if k != "prompt"}

    try:
        result_text = process_request(msgs, **extra)
    except Exception as e:
        logger.exception("process_request failed")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_error"}},
        )

    prompt_tokens = _estimate_tokens(req.prompt)
    completion_tokens = _estimate_tokens(result_text)

    if req.stream:
        async def generate():
            yield f"data: {json.dumps({'id': 'cmpl-x', 'object': 'text_completion.chunk', 'created': int(time.time()), 'model': req.model, 'choices': [{'text': result_text, 'finish_reason': None}]})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")

    return JSONResponse(content={
        "id": f"cmpl-{uuid.uuid4().hex[:24]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{"text": result_text, "index": 0, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": "default",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "local",
        }],
    }


# ──────────────────────────────────────────────
# 通用处理接口
# ──────────────────────────────────────────────

@app.post("/process")
async def process(req: ProcessRequest):
    """通用处理接口：接收任意 data，返回处理结果。"""
    msgs = [Message(role="user", content=str(req.data))]
    try:
        result = process_request(msgs, options=req.options)
    except Exception as e:
        logger.exception("process failed")
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})
    return {"success": True, "result": result, "timestamp": int(time.time())}


# ──────────────────────────────────────────────
# 健康检查 & 根路由
# ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "time": int(time.time())}


@app.get("/")
async def root():
    return {
        "service": "api-server",
        "version": "1.1.0",
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
    logger.info(f"Starting API server on port {API_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
