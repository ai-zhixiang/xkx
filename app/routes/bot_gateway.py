#!/usr/bin/env python3
"""
bot_gateway.py — 微信 Bot 网关
接收 unified_connector 的 webhook，转发到 Hermes Bridge (:8642)
"""
import asyncio
import json
import logging
import os
import sys
import time
import httpx
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("bot-gateway")

app = FastAPI(title="Bot Gateway")

# ── 配置 ──
HERMES_API_URL = "http://127.0.0.1:8642/v1/chat/completions"
HERMES_API_KEY = "sk-c86b55fe6d46473fa3deec2f75d0448b"
DB_DSN = "postgresql://lucky:lucky_pass@localhost:5432/xiaolongxia"
MODEL = "hermes-shenzhen"

CONVERSATION_TABLE = "conversation_messages"  # xiaolongxia 库里有这个表
HISTORY_LIMIT = 30  # 注入对话历史条数


# ── Bot 账号查询 ──
async def get_bot_config(bot_id: str) -> Optional[dict]:
    """从 DB 查 Bot 配置（backend 字段决定路由到 Hermes 还是 DeepSeek）"""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            row = await conn.fetchrow(
                "SELECT bot_id, backend, bot_token FROM bot_accounts WHERE bot_id = $1 AND is_active = true",
                bot_id
            )
            if row:
                return dict(row)
            return None
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"get_bot_config({bot_id[:20]}): {e}")
        return None


# ── 对话历史 ──
async def load_history(user_id: str, limit: int = HISTORY_LIMIT) -> list:
    """加载最近对话历史"""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            rows = await conn.fetch(
                f"SELECT role, content FROM {CONVERSATION_TABLE} "
                "WHERE user_id = $1 ORDER BY id DESC LIMIT $2",
                user_id, limit
            )
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        finally:
            await conn.close()
    except Exception as e:
        logger.debug(f"load_history({user_id[:20]}): {e}")
        return []


async def save_message(user_id: str, role: str, content: str):
    """保存一条对话记录"""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            await conn.execute(
                f"INSERT INTO {CONVERSATION_TABLE} (user_id, role, content, created_at) VALUES ($1, $2, $3, NOW())",
                user_id, role, content
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.debug(f"save_message({user_id[:20]}): {e}")


# ── Hermes 调用 ──
async def _call_hermes(
    prompt: str,
    user_id: str,
    user_nickname: str = "",
    openid: str = "",
    user_account_id: int = None,
    media_path: str = "",
) -> str:
    """调用 Hermes Bridge (:8642) — 带 persona + 对话历史"""
    # 构造 system prompt
    media_hint = ""
    if media_path:
        media_hint = f"\n用户发来媒体文件: {media_path}。你可以读取并处理它。"

    system_prompt = (
        f"当前用户: {user_nickname or '铭道'} | OpenID: {(openid or '')[:16]}...{media_hint}\n"
        f"注意：你的 persona 和记忆已加载。用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。"
    )

    # 加载对话历史
    history = await load_history(user_id)
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append(h)
    messages.append({"role": "user", "content": prompt})

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(
                HERMES_API_URL,
                headers={
                    "Authorization": f"Bearer {HERMES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "messages": messages,
                    "max_tokens": 4000,
                },
            )
            if r.status_code != 200:
                logger.error(f"Hermes API error {r.status_code}: {r.text[:200]}")
                return f"抱歉，处理出错（{r.status_code}），请稍后再试。"

            data = r.json()
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not reply:
                reply = "抱歉，暂时无法回复。"

            # 保存对话历史
            await save_message(user_id, "user", prompt)
            await save_message(user_id, "assistant", reply)
            return reply

    except httpx.TimeoutException:
        logger.warning(f"Hermes 超时 user_id={user_id[:20]}")
        return "处理超时，请稍后重试。"
    except Exception as e:
        logger.error(f"_call_hermes: {e}")
        return f"处理出错，请稍后再试。"


# ── 路由分发 ──
async def _route_to_ai(
    bot_id: str,
    user_id: str,
    content: str,
    user_nickname: str = "",
    openid: str = "",
    user_account_id: int = None,
    media_path: str = "",
) -> str:
    """按 backend 字段路由：hermes / deepseek"""
    bot_cfg = await get_bot_config(bot_id)
    backend = (bot_cfg or {}).get("backend", "hermes")

    if backend == "deepseek":
        # 直调 DeepSeek（无记忆、无工具）
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {HERMES_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": content}],
                    },
                )
                if r.status_code == 200:
                    return r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.error(f"deepseek 直调失败: {e}")
            return "DeepSeek 暂时不可用。"

    # 默认走 Hermes
    return await _call_hermes(content, user_id, user_nickname, openid, user_account_id, media_path)


# ── Webhook ──
@app.post("/api/bot/webhook")
async def bot_webhook(request: Request):
    """接收 unified_connector 转发的消息"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    bot_id = data.get("bot_id", "")
    user_id = data.get("user_id", "")
    content = data.get("content", "")
    media_path = data.get("media_path", "")
    nickname = data.get("nickname", data.get("user_nickname", ""))
    openid = data.get("openid", "")
    user_account_id = data.get("user_account_id")

    if not bot_id or not user_id:
        return JSONResponse({"success": False, "error": "Missing bot_id or user_id"}, status_code=400)

    if not content and not media_path:
        return JSONResponse({"success": False, "error": "Empty content"}, status_code=400)

    logger.info(f"📩 {bot_id[:16]} <- {user_id[:20]}: {content[:40]}")

    try:
        response = await _route_to_ai(
            bot_id, user_id, content, nickname, openid, user_account_id, media_path
        )
        logger.info(f"📤 -> {response[:60]}")
        return JSONResponse({"success": True, "response": response})

    except Exception as e:
        logger.error(f"webhook 处理异常: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "bot-gateway"}


# ── 启动 ──
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    logger.info(f"🚀 Bot Gateway 启动 :{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
