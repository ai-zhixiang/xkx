#!/usr/bin/env python3
"""
Agent Connector — Agent 交互层
职责: 收保活层转来的消息 → 调 Hermes → 回结果

不碰 iLink，不碰 session，只管 AI 推理。
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from aiohttp import web

# ══════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════

HTTP_PORT = 9101

# Keepalive Service (发消息用)
KEEPALIVE_URL = "http://127.0.0.1:9100"

# 网关 (Hermes Bridge)
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8001")
GATEWAY_TIMEOUT = 180  # Hermes 最长回复

# ══════════════════════════════════════════════
# 日志
# ══════════════════════════════════════════════

log = logging.getLogger("agent-connector")
log.setLevel(logging.DEBUG)
log.handlers.clear()
fmt = logging.Formatter("%(asctime)s [agent] %(levelname)s %(message)s", datefmt="%H:%M:%S")
sh = logging.StreamHandler(sys.stderr)
sh.setFormatter(fmt)
log.addHandler(sh)

# ══════════════════════════════════════════════
# 暗号匹配 — 本地再检一遍（冗余保护）
# ══════════════════════════════════════════════

import re
from typing import Optional

CONFIG_DIR = Path.home() / "weclaw-1" / "config"
CODE_PHRASE_FILE = CONFIG_DIR / "access_codes.json"

_DEFAULT_CODES = {
    "codes": [
        {"code": "天王盖地虎", "reply": "OpenClaw 是SB！", "level": "admin"},
        {"code": "宝塔镇河妖", "reply": "微侠真牛逼！", "level": "admin"},
        {"code": "微侠真牛逼", "reply": "天王盖地虎！同志！", "level": "admin"},
        {"code": "openclaw是sb", "reply": "宝塔镇河妖！收到！", "level": "admin"},
    ]
}

def _match_code_phrase(text: str):
    stripped = re.sub(r'[，。！？、；：""\'\s]', '', text).lower()
    try:
        codes = json.loads(CODE_PHRASE_FILE.read_text()).get("codes", [])
    except Exception:
        codes = _DEFAULT_CODES["codes"]
    for entry in codes:
        code = re.sub(r'[，。！？、；：""\'\s]', '', entry["code"]).lower()
        if code in stripped:
            return entry
    return None

# ══════════════════════════════════════════════
# 消息处理
# ══════════════════════════════════════════════

async def _call_gateway(bot_id: str, from_user: str, text: str,
                        msg_id: str) -> Optional[str]:
    """调网关（Hermes Bridge）"""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=GATEWAY_TIMEOUT)) as s:
            async with s.post(f"{GATEWAY_URL}/api/bot/webhook", json={
                "bot_id": bot_id,
                "user_id": from_user,
                "content": text,
                "msg_id": msg_id,
            }) as r:
                if r.status == 200:
                    data = await r.json()
                    if data.get("success") and data.get("response"):
                        return data["response"]
    except asyncio.TimeoutError:
        log.warning("网关超时(%ds): %s", GATEWAY_TIMEOUT, from_user[:20])
    except Exception as e:
        log.warning("网关异常: %s", e)
    return None

async def _send_reply(bot_id: str, to_user: str, text: str, ctx: str):
    """通过保活层发消息"""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(f"{KEEPALIVE_URL}/api/send", json={
                "bot_id": bot_id,
                "to_user": to_user,
                "text": text,
                "context_token": ctx,
            }) as r:
                if r.status != 200:
                    log.warning("保活层发消息失败: %d", r.status)
    except Exception as e:
        log.warning("保活层不可达: %s", e)

# ══════════════════════════════════════════════
# HTTP API — 收保活层的消息
# ══════════════════════════════════════════════

async def handle_message(request):
    """保活层 POST 过来的消息"""
    start = time.time()
    try:
        data = await request.json()
        bot_id = data.get("bot_id", "")
        from_user = data.get("from_user", "")
        text = data.get("text", "")
        msg_id = data.get("msg_id", "")
        ctx = data.get("context_token", "")

        if not text:
            return web.json_response({"reply": None})

        log.info("📩 %s: %s", from_user[:20], text[:60])

        # 再次检查暗号（保活层已经查过，但冗余保护）
        code_match = _match_code_phrase(text)
        if code_match:
            log.info("🔐 暗号(Agent层兜底): %s", code_match["reply"])
            # 暗号在保活层已经回了，这里不留痕迹
            return web.json_response({"reply": None})

        # 调网关
        reply = await _call_gateway(bot_id, from_user, text, msg_id)

        elapsed = time.time() - start
        if reply:
            log.info("📤 回复(%dms): %s", int(elapsed * 1000), reply[:40])
        else:
            log.warning("⚠️ 网关无回复(%dms)", int(elapsed * 1000))

        return web.json_response({"reply": reply or None})

    except Exception as e:
        log.exception("handle_message 异常: %s", e)
        return web.json_response({"reply": None, "error": str(e)}, status=500)

async def handle_health(request):
    return web.json_response({
        "ok": True,
        "uptime": time.time(),
    })

# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
# Safe Terminal — 受限脚本执行
# ══════════════════════════════════════════════

ALLOWED_COMMANDS = {"python3", "python", "ls", "cat", "head", "tail", "wc", "du", "file", "stat", "pwd", "echo", "mkdir", "which", "pip3", "cp", "mv", "rm"}

async def handle_terminal(request):
    """安全受限的脚本执行端点。
    仅允许在用户 download 目录下运行指定命令。
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    user_id = (body.get("user_id") or "").strip()
    command = (body.get("command") or "").strip()
    args = body.get("args") or []

    if not user_id or not command:
        return web.json_response({"error": "missing user_id or command"}, status=400)

    # 只允许白名单命令
    cmd_base = command.split("/")[-1] if "/" in command else command
    if cmd_base not in ALLOWED_COMMANDS:
        return web.json_response({"error": f"command not allowed: {cmd_base}"}, status=403)

    # 检查路径安全: 只能在用户下载目录
    download_dir = Path("/home/ubuntu/weclaw-keepalive/downloads") / user_id
    safe_dir = download_dir.resolve()

    # 构造完整命令
    full_cmd = [command] + args

    # 额外安全检查：确保所有路径参数都在 safe_dir 内
    for arg in args:
        if arg.startswith("/"):
            arg_path = Path(arg).resolve()
            if not str(arg_path).startswith(str(safe_dir)):
                return web.json_response({"error": f"path not allowed: {arg}"}, status=403)

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(safe_dir),
            env={**os.environ, "HOME": str(safe_dir)},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        return web.json_response({
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
        })
    except asyncio.TimeoutError:
        proc.kill()
        return web.json_response({"error": "timeout"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# 添加路由
async def main():
    log.info("=" * 50)
    log.info("Agent Connector 启动 :9101")
    log.info("  网关: %s", GATEWAY_URL)
    log.info("  保活层: %s", KEEPALIVE_URL)
    log.info("=" * 50)

    app = web.Application()
    app.router.add_post("/api/message", handle_message)
    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/api/terminal/run", handle_terminal)
    app.router.add_get("/api/terminal/run", handle_terminal)  # GET 方便测试

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log.info("agent-connector ready on :%d", HTTP_PORT)

    # 保持运行
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
