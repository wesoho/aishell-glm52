"""
OpenAI 兼容 API 服务
- POST /v1/chat/completions  (OpenAI 标准聊天接口)
- GET  /v1/models            (模型列表)
- GET  /health               (健康检查)
- POST /process              (通用处理接口)

处理逻辑在 process_request() 中，可自由修改。
"""

import time
import uuid
import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

app = FastAPI(title="API Server", version="1.0.0")

# ──────────────────────────────────────────────
# 请求/响应模型 (OpenAI 兼容)
# ──────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: Any  # str or list (vision)

class ChatCompletionRequest(BaseModel):
    model: str = "default"
    messages: List[Message]
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    model_config = ConfigDict(extra="allow")

class ProcessRequest(BaseModel):
    data: Any
    options: Optional[Dict[str, Any]] = None

# ──────────────────────────────────────────────
# 核心处理逻辑 — 在这里自定义你的处理方式
# ──────────────────────────────────────────────

def process_request(messages: List[Message], **kwargs) -> str:
    """
    处理用户请求，返回结果文本。
    当前实现：拼接所有消息内容并回显。
    替换此函数即可实现任意处理逻辑。
    """
    parts = []
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content, ensure_ascii=False)
        parts.append(f"[{msg.role}] {content}")
    return "\n".join(parts)


# ──────────────────────────────────────────────
# OpenAI 兼容接口
# ──────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    # 处理请求（排除 messages 避免重复传参）
    extra = {k: v for k, v in req.model_dump().items() if k != "messages"}
    result_text = process_request(req.messages, **extra)

    # 构造 OpenAI 兼容响应
    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result_text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": sum(len(str(m.content)) for m in req.messages),
            "completion_tokens": len(result_text),
            "total_tokens": sum(len(str(m.content)) for m in req.messages) + len(result_text),
        },
    }
    return JSONResponse(content=response)


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
    result = process_request(msgs)
    return {"success": True, "result": result, "timestamp": int(time.time())}


# ──────────────────────────────────────────────
# 健康检查
# ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "time": int(time.time())}


@app.get("/")
async def root():
    return {"service": "api-server", "version": "1.0.0", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
