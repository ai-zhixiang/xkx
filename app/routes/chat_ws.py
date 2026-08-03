"""
享客虾 WebSocket Chat — Android App 实时对话

App 连接: wss://ai.pangoozn.com/api/chat/ws?token=<token>
消息格式: {"message":"用户输入"}
回复格式: {"content":"AI回复","actions":[...]}
"""
import os
import json
import logging
import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import jwt

logger = logging.getLogger(__name__)
router = APIRouter()

HERMES_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:9101/api/message")

# 和 auth-center 共享 JWT 密钥
JWT_SECRET = "sk-auth-e6d7fe701ed753da3c6e396900cc249445aac554315162e0d970900b08045e90"
JWT_ALGO = "HS256"


async def verify_token(token: str) -> str | None:
    """本地解码 JWT 提取 openid/phone 作为 user_id"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload.get("openid") or payload.get("phone")
    except Exception as e:
        logger.warning(f"[ChatWS] token 校验失败: {e}")
    return None


@router.websocket("/api/chat/ws")
async def chat_websocket(websocket: WebSocket, token: str = Query("")):
    await websocket.accept()

    # 验证 token
    user_id = await verify_token(token)
    if not user_id:
        await websocket.send_json({"error": "token无效，请重新登录"})
        await websocket.close(code=4001)
        return

    logger.info(f"[ChatWS] 用户 {user_id} 已连接")

    try:
        while True:
            # 接收客户端消息
            data = await websocket.receive_json()
            message = data.get("message", "")

            if not message.strip():
                continue

            logger.info(f"[ChatWS] {user_id}: {message[:50]}")

            # 转发到 Hermes Agent
            try:
                async with httpx.AsyncClient(timeout=60) as c:
                    resp = await c.post(HERMES_URL, json={
                        "user_id": user_id,
                        "message": message,
                        "source": "android_app",
                    })
                    if resp.status_code == 200:
                        hermes_reply = resp.json()
                        # 将 Hermes 回复映射为 App 期望的格式
                        content = hermes_reply.get("reply") or hermes_reply.get("content") or ""
                        if not content:
                            content = "嗯，我在听。你说。"
                        await websocket.send_json({
                            "content": content,
                            "role": "assistant",
                            "done": True,
                        })
                    else:
                        await websocket.send_json({
                            "error": f"服务异常: {resp.status_code}"
                        })
            except Exception as e:
                logger.error(f"[ChatWS] Hermes 请求失败: {e}")
                await websocket.send_json({"error": "AI服务暂不可用"})

    except WebSocketDisconnect:
        logger.info(f"[ChatWS] 用户 {user_id} 断开")
    except Exception as e:
        logger.warning(f"[ChatWS] 异常: {e}")


@router.post("/api/chat/send")
async def chat_send(data: dict):
    """Android App HTTP POST 发送消息"""
    message = data.get("message", "")
    user_id = data.get("user_id", "unknown")
    if not message.strip():
        return {"reply": "", "role": "assistant"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            resp = await c.post(HERMES_URL, json={
                "user_id": user_id,
                "message": message,
                "source": "android_app",
            })
            if resp.status_code == 200:
                hermes = resp.json()
                content = hermes.get("reply") or hermes.get("content") or "嗯，我在听。你说。"
                return {"reply": content, "role": "assistant"}
            return {"reply": "服务异常", "role": "assistant"}
    except Exception as e:
        logger.error(f"[ChatSend] Hermes 请求失败: {e}")
        return {"reply": "AI服务暂不可用", "role": "assistant"}

