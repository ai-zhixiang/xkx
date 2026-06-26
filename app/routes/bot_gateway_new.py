"""
享客虾 Bot 网关 — iLink 微信机器人统一管理

架构：
  用户微信 → iLink Bot → Hermes(连接层) → 享客虾 webhook(大脑)

每个 Bot 在 Hermes 上维护 WebSocket 连接，
消息通过 webhook 转发到享客虾进行处理。
"""

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import logging
import json
import os
import sys
import time
import httpx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bot", tags=["Bot 网关"])

# 🔮 主脑会话追踪：user_id → 最后活动时间戳
_master_sessions: dict[str, float] = {}
_MASTER_TIMEOUT = 600  # 10 分钟无消息自动切回微侠

# 管理员白名单（跳过绑定检查）
_ADMIN_USERS = {
    "o9cq806n88EiZCsWOatm",     # 铭道
}

# ===== 数据库操作 =====

BOT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bot_accounts (
    id SERIAL PRIMARY KEY,
    bot_id VARCHAR(100) UNIQUE NOT NULL,           -- iLink Bot ID
    bot_token VARCHAR(255) NOT NULL,                -- iLink Bot Token
    user_id VARCHAR(100) NOT NULL,                  -- 绑定微信号
    nickname VARCHAR(100) DEFAULT '',               -- 用户昵称
    backend VARCHAR(50) DEFAULT 'hermes',           -- 后端类型: hermes | deepseek
    backend_url VARCHAR(255) DEFAULT '',            -- 自定义后端 URL
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
"""


# ===== Pydantic Models =====

class BotRegisterRequest(BaseModel):
    bot_id: str
    bot_token: str
    user_id: str
    nickname: str = ""
    backend: str = "hermes"

class BotRegisterResponse(BaseModel):
    success: bool
    bot_id: str
    message: str

class BotListResponse(BaseModel):
    bots: list[dict]
    total: int


# ===== 短链接绑定 =====

import secrets as _secrets
import string as _string
_bind_codes: dict = {}  # code → {"channel_type": str, "channel_user_id": str}

def _generate_bind_code() -> str:
    """生成 6 位短码"""
    chars = _string.ascii_letters + _string.digits
    while True:
        code = ''.join(_secrets.choice(chars) for _ in range(6))
        if code not in _bind_codes:
            return code

@router.get("/b/{code}")
async def bind_redirect(code: str):
    """短链接重定向到 OAuth 绑定"""
    info = _bind_codes.get(code)
    if not info:
        return HTMLResponse("<h3>❌ 链接已失效，请重新扫码绑定</h3>", status_code=404)
    channel_type = info["channel_type"]
    channel_user_id = info["channel_user_id"]
    # 跳转到 hai.html，前端 JS 检测 bind 参数后自动触发 OAuth
    import urllib.parse
    from fastapi.responses import RedirectResponse
    bind_param = f"{channel_type}:{channel_user_id}"
    redirect_url = f"https://hai.pangoozn.com/static/hai.html?bind={urllib.parse.quote(bind_param)}"
    return RedirectResponse(url=redirect_url)


@router.post("/bind")
async def bind_channel(data: dict):
    """外部调用的通道绑定（OAuth 回调后写入）"""
    channel_type = data.get("channel_type", "ilink")
    channel_user_id = data.get("channel_user_id", "")
    openid = data.get("openid", "")
    nickname = data.get("nickname", "")
    if not channel_user_id:
        return {"success": False, "error": "缺少 channel_user_id"}
    try:
        from app.models import AsyncSessionLocal as async_session_factory
        from sqlalchemy import text as sa_text
        async with async_session_factory() as session:
            existing = await session.execute(
                sa_text("SELECT id FROM channel_bindings WHERE channel_type = :ct AND channel_user_id = :cuid"),
                {"ct": channel_type, "cuid": channel_user_id},
            )
            if existing.fetchone():
                await session.execute(
                    sa_text("UPDATE channel_bindings SET openid = :oid, nickname = :nick, updated_at = NOW() WHERE channel_type = :ct AND channel_user_id = :cuid"),
                    {"oid": openid, "nick": nickname, "ct": channel_type, "cuid": channel_user_id},
                )
            else:
                await session.execute(
                    sa_text("""INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname, created_at, updated_at)
                        VALUES (:ct, :cuid, :oid, :nick, NOW(), NOW())"""),
                    {"ct": channel_type, "cuid": channel_user_id, "oid": openid, "nick": nickname},
                )
            await session.commit()
        return {"success": True, "message": f"通道 {channel_type}:{channel_user_id[:20]}... 已绑定 {nickname}"}
    except Exception as e:
        logger.exception(f"通道绑定失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/bind/code")
async def create_bind_code(data: dict):
    """生成绑定短码"""
    channel_type = data.get("channel_type", "ilink")
    channel_user_id = data.get("channel_user_id", "")
    if not channel_user_id:
        return {"success": False, "error": "缺少 channel_user_id"}
    code = _generate_bind_code()
    _bind_codes[code] = {"channel_type": channel_type, "channel_user_id": channel_user_id}
    return {"success": True, "code": code}


# ===== Endpoints =====

@router.post("/register", response_model=BotRegisterResponse)
async def register_bot(req: BotRegisterRequest):
    """注册一个新的微信 Bot 凭证"""
    from app.models import AsyncSessionLocal as async_session_factory
    import sqlalchemy as sa
    from sqlalchemy import text as sa_text

    try:
        async with async_session_factory() as session:
            # 检查是否已存在
            existing = await session.execute(
                sa_text("SELECT id FROM bot_accounts WHERE bot_id = :bid"),
                {"bid": req.bot_id},
            )
            if existing.fetchone():
                # 更新 token
                await session.execute(
                    sa_text("""
                        UPDATE bot_accounts 
                        SET bot_token = :token, user_id = :uid, nickname = :nick, 
                            updated_at = NOW(), is_active = true
                        WHERE bot_id = :bid
                    """),
                    {"token": req.bot_token, "uid": req.user_id, 
                     "nick": req.nickname, "bid": req.bot_id},
                )
                msg = "Bot 已更新"
            else:
                await session.execute(
                    sa_text("""
                        INSERT INTO bot_accounts (bot_id, bot_token, user_id, nickname, backend)
                        VALUES (:bid, :token, :uid, :nick, :backend)
                    """),
                    {"bid": req.bot_id, "token": req.bot_token, 
                     "uid": req.user_id, "nick": req.nickname, 
                     "backend": req.backend},
                )
                msg = "Bot 注册成功"
            await session.commit()
            return BotRegisterResponse(success=True, bot_id=req.bot_id, message=msg)
    except Exception as e:
        logger.exception(f"Bot 注册失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
async def list_bots():
    """列出所有注册的 Bot"""
    from app.models import AsyncSessionLocal as async_session_factory
    import sqlalchemy as sa
    from sqlalchemy import text as sa_text

    try:
        async with async_session_factory() as session:
            rows = await session.execute(
                sa_text("""
                    SELECT bot_id, user_id, nickname, backend, is_active, created_at
                    FROM bot_accounts ORDER BY created_at DESC
                """),
            )
            bots = []
            for r in rows.fetchall():
                bots.append({
                    "bot_id": r[0],
                    "user_id": r[1],
                    "nickname": r[2] or "",
                    "backend": r[3],
                    "is_active": r[4],
                    "created_at": r[5].isoformat() if r[5] else "",
                })
            return BotListResponse(bots=bots, total=len(bots))
    except Exception as e:
        logger.exception(f"Bot 列表查询失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{bot_id}/deactivate")
async def deactivate_bot(bot_id: str):
    """停用某个 Bot"""
    from app.models import AsyncSessionLocal as async_session_factory
    from sqlalchemy import text as sa_text

    try:
        async with async_session_factory() as session:
            await session.execute(
                sa_text("UPDATE bot_accounts SET is_active = false, updated_at = NOW() WHERE bot_id = :bid"),
                {"bid": bot_id},
            )
            await session.commit()
            return {"success": True, "bot_id": bot_id, "message": "Bot 已停用"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qrcode")
async def generate_qrcode():
    """生成 iLink 微信 Bot 二维码"""
    qr = await _fetch_qrcode()
    if not qr:
        return JSONResponse({"success": False, "error": "生成失败"}, status_code=502)
    # 同时生成图片
    await _save_qr_image(qr["qrcode_url"])
    return {"success": True, **qr}


@router.get("/qrcode/image")
async def qrcode_image(value: str = None):
    """返回二维码图片\n
    如果不传 value，生成新的；传 value 则用指定的值生成图片
    """
    import qrcode as qrlib
    from io import BytesIO
    from fastapi.responses import Response

    if value:
        qr_data = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={value}&bot_type=3"
    else:
        qr = await _fetch_qrcode()
        if not qr:
            return JSONResponse({"success": False, "error": "生成失败"}, status_code=502)
        qr_data = qr["qrcode_url"]

    img = qrlib.make(qr_data)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/qrcode/status")
async def check_qrcode_status(qrcode: str):
    """检查二维码扫描状态，已过期自动刷新"""
    return await _poll_single_qrcode(qrcode)


async def _fetch_qrcode() -> Optional[dict]:
    """从 iLink 获取一张新二维码（带 local_token_list 告知需要替换的旧 Bot）"""
    import httpx
    base_url = "https://ilinkai.weixin.qq.com"
    url = f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3"
    headers = {
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 4),  # OpenClaw 2.4.4
        "Content-Type": "application/json",
    }
    # 收集已有 Bot token 告知 iLink，这样扫码时才会替换旧 Bot
    local_tokens = _get_existing_bot_tokens()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json={"local_token_list": local_tokens})
            if resp.status_code != 200:
                logger.error(f"QR fetch failed: HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            qrcode_value = str(data.get("qrcode") or "")
            qrcode_url = str(data.get("qrcode_img_content") or "")
            if not qrcode_value:
                return None
            return {
                "qrcode_value": qrcode_value,
                "qrcode_url": qrcode_url,
                "qrcode_img": f"https://hai.pangoozn.com/api/bot/qrcode/image",
            }
    except Exception as e:
        logger.exception("生成二维码失败")
        return None


def _get_existing_bot_tokens() -> list:
    """收集所有已有 Bot 的 token，用于 QR 码请求中的 local_token_list"""
    tokens = []
    # 从 xiaolongxia DB 读已有 bot tokens
    try:
        import sqlalchemy as sa
        engine = sa.create_engine("postgresql://lucky:lucky_pass@localhost:5432/xiaolongxia")
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT bot_token FROM bot_accounts WHERE is_active = true AND bot_token IS NOT NULL")
            )
            for row in rows:
                t = row[0].strip() if row[0] else ""
                if t:
                    tokens.append(t)
        engine.dispose()
    except Exception as e:
        logger.warning(f"读取已有 Bot tokens 失败: {e}")
    return tokens


async def _save_qr_image(qrcode_url: str):
    """保存二维码图片到静态目录"""
    import qrcode as qrlib
    import os
    try:
        img = qrlib.make(qrcode_url)
        path = "/home/ubuntu/ailuckycards/app/static/bot_qrcode.png"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.save(path)
    except Exception as e:
        logger.warning(f"保存QR图片失败: {e}")


# ===== Session 级别的二维码追踪 =====
import uuid as _uuid_lib
_qr_sessions: dict = {}  # session_id → {"qrcodes": [value1, value2, ...], "result": None or dict}

@router.get("/qrcode/session")
async def create_qr_session():
    """创建一个二维码绑定 Session（绑定过程中所有 QR 码共享同一个 session）"""
    session_id = str(_uuid_lib.uuid4()).replace("-", "")[:16]
    qr = await _fetch_qrcode()
    if not qr:
        return {"success": False, "error": "生成二维码失败"}
    _qr_sessions[session_id] = {
        "qrcodes": [qr["qrcode_value"]],
        "result": None,
    }
    await _save_qr_image(qr["qrcode_url"])
    return {
        "success": True,
        "session_id": session_id,
        **qr,
    }


@router.get("/qrcode/session-poll")
async def poll_qr_session(session_id: str = ""):
    """轮询 Session 下所有 QR 码的状态"""
    session = _qr_sessions.get(session_id)
    if not session:
        return {"success": False, "error": "session not found"}
    
    # 如果已有结果，直接返回
    if session["result"]:
        return session["result"]
    
    # 逐个检查每个 QR 码（从最新的开始，旧的可能已被扫描确认）
    for qrcode in reversed(session["qrcodes"]):
        result = await _poll_single_qrcode(qrcode)
        if result.get("scanned"):
            session["result"] = result
            return result
        if result.get("expired"):
            # 这个码过期了，跳过
            continue
    
    # 最后一个码过期了？生成新的
    last_qr = session["qrcodes"][-1] if session["qrcodes"] else ""
    if last_qr:
        # 检查最后一个是否过期
        last_result = await _poll_single_qrcode(last_qr)
        if last_result.get("expired"):
            new_qr = await _fetch_qrcode()
            if new_qr:
                session["qrcodes"].append(new_qr["qrcode_value"])
                await _save_qr_image(new_qr["qrcode_url"])
                return {
                    "success": True, "scanned": False,
                    "expired": True, "refreshed": True,
                    "session_id": session_id, **new_qr
                }
    
    return {"success": True, "scanned": False, "status": "wait"}


async def _poll_single_qrcode(qrcode: str) -> dict:
    """轮询二维码状态，过期自动刷新"""
    import httpx
    base_url = "https://ilinkai.weixin.qq.com"
    headers = {
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 4),  # OpenClaw 2.4.4
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            status_url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
            try:
                resp = await client.get(status_url, headers=headers)
                data = resp.json()
            except (httpx.TimeoutException, httpx.ReadTimeout):
                # 长轮询超时正常，说明还没人扫
                return {"success": True, "scanned": False, "status": "wait"}
            except Exception:
                return {"success": True, "scanned": False, "status": "wait"}

            status = str(data.get("status") or "wait")

            if status == "scaned":
                # 已扫码但未确认，继续等
                return {"success": True, "scanned": False, "status": "scaned"}

            if status == "confirmed" or (data.get("ilink_bot_id") and data.get("bot_token")):
                bot_id = str(data.get("ilink_bot_id") or data.get("bot_id") or "")
                bot_token = str(data.get("bot_token") or "")
                nickname = str(data.get("nickname", ""))
                user_id = str(data.get("ilink_user_id") or data.get("user_id", ""))
                if bot_id and bot_token:
                    return {
                        "success": True,
                        "scanned": True,
                        "bot_id": bot_id,
                        "bot_token": bot_token,
                        "nickname": nickname,
                        "user_id": user_id,
                    }
                return {"success": True, "scanned": True, "bot_id": bot_id}

            if status == "expired":
                new_qr = await _fetch_qrcode()
                if new_qr:
                    return {"success": True, "scanned": False, "expired": True, "refreshed": True, **new_qr}
                return {"success": True, "scanned": False, "expired": True, "refreshed": False}
            elif status == "scaned_but_redirect":
                return {"success": True, "scanned": False, "status": "scaned_but_redirect"}
            else:
                # wait - 还在等
                return {"success": True, "scanned": False, "status": "wait"}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/activate")
async def activate_bot(data: dict):
    """扫码确认后：注册 Bot + 启动连接器"""
    bot_id = data.get("bot_id", "")
    bot_token = data.get("bot_token", "")
    nickname = data.get("nickname", "")
    user_id = data.get("user_id", "")
    
    if not bot_id or not bot_token:
        return {"success": False, "error": "缺少 bot_id 或 bot_token"}
    
    # 1. 注册到 DB
    try:
        from app.models import AsyncSessionLocal as async_session_factory
        from sqlalchemy import text as sa_text
        async with async_session_factory() as session:
            existing = await session.execute(
                sa_text("SELECT id FROM bot_accounts WHERE bot_id = :bid"),
                {"bid": bot_id},
            )
            if existing.fetchone():
                await session.execute(
                    sa_text("""UPDATE bot_accounts SET bot_token = :token, user_id = :uid, 
                        nickname = COALESCE(NULLIF(:nick, ''), nickname), updated_at = NOW(), is_active = true
                        WHERE bot_id = :bid"""),
                    {"token": bot_token, "uid": user_id, "nick": nickname, "bid": bot_id},
                )
            else:
                    await session.execute(
                        sa_text("""INSERT INTO bot_accounts (bot_id, bot_token, user_id, nickname, backend)
                            VALUES (:bid, :token, :uid, :nick, 'hermes')"""),
                    {"bid": bot_id, "token": bot_token, "uid": user_id, "nick": nickname},
                )
            await session.commit()
        logger.info(f"[激活] Bot 注册成功: {bot_id} ({nickname})")
    except Exception as e:
        logger.exception(f"[激活] DB 注册失败: {e}")
        return {"success": False, "error": f"注册失败: {e}"}
    
    # 2. 绑定通道身份（直接用 iLink user_id，不需额外 OAuth）
    try:
        from app.models import AsyncSessionLocal as async_session_factory
        from sqlalchemy import text as sa_text
        async with async_session_factory() as session:
            # 检查是否已绑定
            existing = await session.execute(
                sa_text("SELECT id FROM channel_bindings WHERE channel_type = 'ilink' AND channel_user_id = :cuid"),
                {"cuid": user_id},
            )
            if not existing.fetchone():
                await session.execute(
                    sa_text("""INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname, created_at, updated_at)
                        VALUES ('ilink', :cuid, :cuid, :nick, NOW(), NOW())"""),
                    {"cuid": user_id, "nick": nickname},
                )
                await session.commit()
                logger.info(f"[激活] 通道绑定成功: {user_id[:20]} ({nickname})")
    except Exception as e:
        logger.warning(f"[激活] 通道绑定失败（不影响使用）: {e}")

    # 3. 启动连接器
    try:
        from app.bot.connector import get_connectors_dir
        import subprocess, os
        log_dir = get_connectors_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(log_dir / f"{bot_id}.log")
        pid_file = log_dir / f"{bot_id}.pid"
        
        # 检查是否已运行
        if pid_file.exists():
            try:
                old_pid = int(pid_file.read_text().strip())
                os.kill(old_pid, 15)
                logger.info(f"[激活] 杀旧进程: {old_pid}")
                import time
                time.sleep(1)
            except (OSError, ValueError):
                pass
        
        connector_path = "/home/ubuntu/xiaolongxia/app/bot/connector.py"
        if not os.path.exists(connector_path):
            connector_path = os.path.join(os.path.dirname(__file__), "../bot/connector.py")
            connector_path = os.path.abspath(connector_path)
        
        proc = subprocess.Popen(
            [sys.executable, connector_path, "start", bot_id, 
             "--token", bot_token, "--log", log_file],
            cwd=os.path.dirname(connector_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"[激活] 连接器启动中: {bot_id} (PID={proc.pid})")
        return {"success": True, "bot_id": bot_id, "message": f"Bot {nickname or bot_id} 已激活"}
    except Exception as e:
        logger.exception(f"[激活] 连接器启动失败: {e}")
        return {"success": True, "bot_id": bot_id, "message": f"Bot 已注册但连接器启动失败: {e}"}


@router.get("/connector/status")
async def connector_status():
    """所有 Bot 连接器的运行状态"""
    from app.bot.connector import is_connector_running
    from app.models import AsyncSessionLocal as async_session_factory
    from sqlalchemy import text as sa_text

    try:
        async with async_session_factory() as session:
            rows = await session.execute(
                sa_text("SELECT bot_id, nickname, is_active FROM bot_accounts ORDER BY created_at DESC"),
            )
            bots = []
            for r in rows.fetchall():
                bot_id = r[0]
                bots.append({
                    "bot_id": bot_id,
                    "nickname": r[1] or "",
                    "is_active": r[2],
                    "running": is_connector_running(bot_id),
                })
            return {"bots": bots, "total": len(bots)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{bot_id}/start")
async def start_connector(bot_id: str):
    """启动 Bot 连接器进程"""
    from app.bot.connector import is_connector_running, save_connector_pid
    import subprocess, os

    if is_connector_running(bot_id):
        return {"success": True, "bot_id": bot_id, "message": "已在运行"}

    from app.models import AsyncSessionLocal as async_session_factory
    from sqlalchemy import text as sa_text

    bot_token = ""
    try:
        async with async_session_factory() as session:
            row = await session.execute(
                sa_text("SELECT bot_token FROM bot_accounts WHERE bot_id = :bid AND is_active = true"),
                {"bid": bot_id},
            )
            r = row.fetchone()
            if r:
                bot_token = r[0]
    except Exception:
        pass

    if not bot_token:
        raise HTTPException(status_code=404, detail="Bot 未找到或未激活")

    connector_script = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "bot", "connector.py")
    )
    log_dir = os.path.expanduser("~/.hermes/bot_connectors")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{bot_id}.log")

    try:
        proc = subprocess.Popen(
            [sys.executable, connector_script, "start", bot_id,
             "--token", bot_token, "--log", log_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        import asyncio
        await asyncio.sleep(1)
        if is_connector_running(bot_id):
            return {"success": True, "bot_id": bot_id, "message": "已启动", "log": log_file}
        else:
            return {"success": False, "bot_id": bot_id, "message": "启动失败，查看日志"}
    except Exception as e:
        logger.exception("启动连接器失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{bot_id}/stop")
async def stop_connector(bot_id: str):
    """停止 Bot 连接器进程"""
    from app.bot.connector import read_connector_pid, get_connectors_dir

    pid = read_connector_pid(bot_id)
    if not pid:
        return {"success": False, "bot_id": bot_id, "message": "未运行"}

    import os, signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    (get_connectors_dir() / f"{bot_id}.pid").unlink(missing_ok=True)
    return {"success": True, "bot_id": bot_id, "message": "已停止"}


@router.post("/v1/chat/completions")
async def openai_chat_completions(data: dict):
    """
    OpenAI 兼容的 chat completions 端点。
    让 OpenClaw 可以直接把模型指向享客虾网关。
    """
    messages = data.get("messages", [])
    stream = data.get("stream", False)
    
    if stream:
        return JSONResponse({"error": "Streaming not supported"}, status_code=400)
    
    # 提取最后一条用户消息
    user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_msg = msg.get("content", "")
            break
    
    if not user_msg:
        return JSONResponse({"error": "No user message"}, status_code=400)
    
    # 走网关处理逻辑（暗号匹配 + AI 路由）
    result = await bot_webhook_internal("openclaw-bridge", user_msg, f"openclaw:{data.get('user', 'unknown')}")
    
    response_text = result.get("response", "🤖 处理失败")
    
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model", "xiakexia-gateway"),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": response_text
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
    }


async def bot_webhook_internal(bot_id: str, content: str, user_id: str) -> dict:
    """内部调用：处理消息并返回回复（含额度检查）"""
    if not content:
        return {"success": False, "error": "empty content"}
    
    # 暗号匹配
    code_reply = _match_code_phrase(content)
    if code_reply:
        logger.info(f"[暗号] bot={bot_id} matched (internal)")
        return {"success": True, "response": code_reply}
    
    # 额度检查 + 消耗
    from app.bot.quota import check_and_consume
    quota_result = check_and_consume(user_id)
    
    if not quota_result["ok"]:
        logger.info(f"[Quota] 用户 {user_id[:20]} 额度已用完")
        return {"success": True, "response": quota_result["message"]}
    
    # 路由到 AI
    response = await _route_to_ai(bot_id, user_id, content)
    
    # 回覆过暗号匹配
    code_reply = _match_code_phrase(response)
    if code_reply:
        response = code_reply
    
    # 如果是首条消息，在 AI 回复前加欢迎
    if quota_result.get("welcome"):
        response = quota_result["welcome"] + "\n\n" + response
    
    return {"success": True, "response": f"🦞 {response}"}


@router.post("/webhook")
async def bot_webhook(data: dict):
    """
    Hermes 转发消息的 webhook 入口。
    
    Hermes 收到微信消息后，POST 到此端点：
    {
        "bot_id": "2de655beba43-im-bot",
        "user_id": "o9cq806n...@im.wechat",
        "content": "用户发的消息",
        "msg_type": "text"
    }
    """
    logger.info(f"[Bot Webhook] 收到消息: bot={data.get('bot_id','')} user={data.get('user_id','')[:20]} msg={data.get('content','')[:30]}")
    
    bot_id = data.get("bot_id", "")
    user_id = data.get("user_id", "")
    content = data.get("content", "")
    
    if not content:
        return {"success": False, "error": "empty content"}

    # 暗号匹配（共用函数）
    code_reply = _match_code_phrase(content)
    if code_reply:
        logger.info(f"[暗号] bot={bot_id} matched")
        return {"success": True, "response": code_reply}
    
    now = time.time()
    
    # 🔮 @weclaw 召唤 → 进入主脑会话模式
    if content.strip().startswith("@weclaw"):
        _master_sessions[user_id] = now
        summon_content = content.strip()[len("@weclaw"):].strip()
        if not summon_content:
            return {"success": True, "response": "🧠 主脑在线，直接聊，说 \"退出\" 切回微侠"}
        response = await _call_master_hermes(summon_content, user_id)
        # 回覆过暗号匹配
        code_reply = _match_code_phrase(response)
        if code_reply:
            response = code_reply
        return {"success": True, "response": f"🧠 {response}"}
    
    # 检查是否在主脑会话中
    if user_id in _master_sessions:
        # 超时检查（10 分钟无消息自动切回）
        if now - _master_sessions[user_id] > _MASTER_TIMEOUT:
            del _master_sessions[user_id]
            logger.info(f"[主脑] {user_id[:20]} 会话超时，自动切回微侠")
        else:
            # 检查退出指令
            exit_words = {"退出", "结束", "bye", "exit", "退下"}
            if content.strip().lower() in exit_words:
                del _master_sessions[user_id]
                # 先让主脑告别，再切回微侠
                goodbye = await _call_master_hermes(
                    "用户说退出了。请用一句话简短告别，语气友好。不要说'再见'之外的任何额外内容。",
                    user_id
                )
                return {"success": True, "response": f"🧠 {goodbye}\n\n🦞 已切回微侠模式"}
            # 刷新超时 + 直通主脑
            _master_sessions[user_id] = now
            response = await _call_master_hermes(content, user_id)
            # 回覆过暗号匹配
            code_reply = _match_code_phrase(response)
            if code_reply:
                response = code_reply
            return {"success": True, "response": f"🧠 {response}"}
    
    # 管理员白名单：跳过绑定检查
    if user_id in _ADMIN_USERS:
        bound_info = {"bound": True, "openid": user_id, "nickname": "铭道"}
    else:
        # == 通道绑定检查 ==
        # 检测用户是否已绑定（ilink 通道的 user_id 即为 channel_user_id）
        channel_type = "ilink"
        channel_user_id = user_id
        bound_info = await _check_binding(channel_type, channel_user_id)
    
    if not bound_info.get("bound"):
        # 未绑定 → 生成短码 OAuth 绑定链接（避免长链接被微信截断）
        code = _generate_bind_code()
        _bind_codes[code] = {"channel_type": channel_type, "channel_user_id": channel_user_id}
        short_url = f"https://hai.pangoozn.com/api/bot/b/{code}"
        bind_msg = (
            "🔐 您还未绑定微信账号，请点击下方链接完成绑定：\n\n"
            f"{short_url}\n\n"
            "绑定后即可使用全部功能 🎉"
        )
        return {"success": True, "response": bind_msg}
    
    # 已绑定 → 注入用户身份
    openid = bound_info.get("openid", "")
    nickname = bound_info.get("nickname", "")
    if nickname:
        logger.info(f"[Bot Webhook] 已识别用户: {nickname} (openid={openid[:15]}...)")

    # 会员状态检查（过期 → 自动注入续费文案）
    membership = await _check_membership(openid)
    prefix = _membership_prefix(membership["status"], membership["days_left"], openid)
    suffix = _membership_suffix(membership["status"], membership["days_left"], openid)

    if prefix:
        logger.info(f"[会员] {nickname} {membership['status']} ({membership['days_left']}天) → 注入提醒")

    # 路由到对应的 AI 后端（微侠本地处理）
    response = await _route_to_ai(bot_id, user_id, content)
    response = prefix + response + suffix
    
    return {
        "success": True,
        "response": f"🦞 {response}",
    }


async def _check_binding(channel_type: str, channel_user_id: str) -> dict:
    """检查通道绑定状态（直接查 DB，不走 HTTP 避免死锁）"""
    try:
        from app.models import AsyncSessionLocal as async_session_factory
        from sqlalchemy import text as sa_text
        async with async_session_factory() as session:
            row = await session.execute(
                sa_text("SELECT openid, nickname FROM channel_bindings WHERE channel_type = :ct AND channel_user_id = :cuid"),
                {"ct": channel_type, "cuid": channel_user_id},
            )
            r = row.fetchone()
            if r:
                return {"bound": True, "openid": r[0], "nickname": r[1] or ""}
    except Exception as e:
        logger.warning(f"[绑定检查] DB 查询失败: {e}")
    return {"bound": False}


# ── 短信验证码接口 ──
import random as _random

SMS_ACCESS_KEY_ID = os.getenv("SMS_ACCESS_KEY_ID", "")
SMS_ACCESS_KEY_SECRET = os.getenv("SMS_ACCESS_KEY_SECRET", "")
SMS_SIGN_NAME = os.getenv("SMS_SIGN_NAME", "中赢智享教育科技深圳")
SMS_TEMPLATE_CODE = os.getenv("SMS_TEMPLATE_CODE", "SMS_506140441")
_verify_codes: dict = {}  # code → {"phone": str, "expires_at": float}
CODE_TTL = 300

@router.post("/send-code")
async def send_sms_code(data: dict):
    """发送短信验证码（走完整阿里云 SDK，mock 模式仅不发真实短信）"""
    phone = (data.get("phone") or "").strip()
    if not phone or not phone.isdigit() or len(phone) < 7:
        return {"ok": False, "msg": "无效手机号"}

    code = str(_random.randint(100000, 999999))
    _verify_codes[code] = {"phone": phone, "expires_at": time.time() + CODE_TTL}

    # 阿里云 SDK 走完整路径，最后一步根据 SMS_MOCK 决定是否真实发送
    sms_mock = os.getenv("SMS_MOCK", "true").lower() in ("true", "1", "yes")
    ok = True
    if not sms_mock:
        try:
            from aliyunsdkcore.client import AcsClient
            from aliyunsdkdysmsapi.request.v20170525 import SendSmsRequest
            import json as _json
            client = AcsClient(SMS_ACCESS_KEY_ID, SMS_ACCESS_KEY_SECRET, "cn-hangzhou")
            req = SendSmsRequest.SendSmsRequest()
            req.set_PhoneNumbers(phone)
            req.set_SignName(SMS_SIGN_NAME)
            req.set_TemplateCode(SMS_TEMPLATE_CODE)
            req.set_TemplateParam(_json.dumps({"code": code}))
            resp = client.do_action_with_exception(req)
            resp_data = _json.loads(resp)
            ok = resp_data.get("Code") == "OK"
            if not ok:
                logger.error(f"[SMS] 发送失败: {resp_data}")
        except Exception as e:
            logger.error(f"[SMS] 异常: {e}")
            ok = False
    else:
        logger.info(f"[SMS-MOCK] 验证码 {code} → {phone[:3]}****{phone[-4:]}")

    result = {"ok": ok, "msg": "已发送" if ok else "短信发送失败，请重试"}
    if sms_mock:
        result["mock_code"] = code
    return result


@router.post("/verify-code")
async def verify_sms_code(data: dict):
    """验证短信验证码 + 绑定手机号 + 关联 openid"""
    phone = data.get("phone", "").strip()
    code = data.get("code", "").strip()
    openid = data.get("openid", "").strip()
    nickname = data.get("nickname", "用户")[:30]

    if not phone or not code:
        return {"ok": False, "msg": "参数不完整"}

    # 验证验证码
    code_info = _verify_codes.get(code)
    if not code_info or code_info["phone"] != phone or code_info["expires_at"] < time.time():
        return {"ok": False, "msg": "验证码错误或已过期"}

    del _verify_codes[code]

    try:
        from app.models import AsyncSessionLocal as _sf
        from sqlalchemy import text as _t
        async with _sf() as _s:
            # 查询或创建用户
            row = await _s.execute(
                _t("SELECT id FROM subscribers WHERE openid = :oid"),
                {"oid": openid or phone},
            )
            r = row.fetchone()
            if r:
                sub_id = r[0]
                await _s.execute(
                    _t("UPDATE subscribers SET nickname = :nick WHERE id = :id"),
                    {"nick": nickname or "虾友", "id": sub_id},
                )
            else:
                # 新用户创建 subscribers 记录（VISITOR 状态）
                from datetime import date, timedelta
                from app.models import SubscriberStatus
                today = date.today()
                row = await _s.execute(
                    _t("""INSERT INTO subscribers (openid, nickname, status, started_at, expires_at, plan_id, messages_limit, trial_used)
                        VALUES (:oid, :nick, :st, :sd, :ed, 4, 500, false)
                        RETURNING id"""),
                    {"oid": openid or phone, "nick": nickname or "虾友",
                     "st": SubscriberStatus.VISITOR.value,
                     "sd": today, "ed": today + timedelta(days=7)},
                )
                sub_id = row.fetchone()[0]

            # 更新 channel_bindings（如果传入 openid）
            if openid:
                await _s.execute(
                    _t("""INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname)
                        VALUES ('ilink', :cuid, :oid, :nick)
                        ON CONFLICT (channel_type, channel_user_id)
                        DO UPDATE SET openid = :oid2, nickname = :nick2"""),
                    {"cuid": openid, "oid": openid, "nick": nickname or "虾友",
                     "oid2": openid, "nick2": nickname or "虾友"},
                )

            await _s.commit()
            return {"ok": True, "msg": "绑定成功", "openid": openid or phone, "subscriber_id": sub_id}
    except Exception as e:
        logger.exception(f"[verify-code] 绑定失败: {e}")
        return {"ok": False, "msg": f"绑定失败: {str(e)[:50]}"}


@router.get("/member-status")
async def member_status(openid: str = ""):
    """查会员状态"""
    result = await _check_membership(openid)
    return result


@router.get("/check-bound")
async def check_bound(openid: str = ""):
    """检查 openid 是否已绑定 Bot"""
    try:
        from app.models import AsyncSessionLocal as _sf
        from sqlalchemy import text as _t
        async with _sf() as _s:
            row = await _s.execute(
                _t("SELECT bot_id FROM bot_accounts WHERE user_id = :uid AND is_active = true LIMIT 1"),
                {"uid": openid},
            )
            r = row.fetchone()
            if r:
                return {"bound": True, "channel_user_id": openid}
    except Exception:
        pass
    return {"bound": False}
# 每日提醒记录: {openid: "YYYY-MM-DD"}
_remind_today: dict[str, str] = {}

async def _check_membership(openid: str) -> dict:
    """查询会员状态。返回:
    {
        'status': 'active'|'expiring'|'expired'|'none',
        'days_left': int, 'expires_at': str or None
    }
    """
    from datetime import date, datetime
    try:
        from app.models import AsyncSessionLocal as sf
        from sqlalchemy import text as t
        async with sf() as s:
            row = await s.execute(
                t("SELECT status, expires_at FROM subscribers WHERE openid = :oid ORDER BY expires_at DESC LIMIT 1"),
                {"oid": openid},
            )
            r = row.fetchone()
            if not r:
                return {"status": "none", "days_left": 0, "expires_at": None}

            status = r[0]
            expires = r[1]  # date object
            if not expires:
                return {"status": "none", "days_left": 0, "expires_at": None}

            today = date.today()
            days_left = (expires - today).days

            if status in ("ACTIVE", "TRIAL"):
                if days_left <= 0:
                    return {"status": "expired", "days_left": 0, "expires_at": expires.isoformat()}
                elif days_left <= 3:
                    return {"status": "expiring", "days_left": days_left, "expires_at": expires.isoformat()}
                else:
                    return {"status": "active", "days_left": days_left, "expires_at": expires.isoformat()}
            elif status == "EXPIRED":
                return {"status": "expired", "days_left": 0, "expires_at": expires.isoformat()}
            else:
                return {"status": "none", "days_left": 0, "expires_at": None}
    except Exception as e:
        logger.warning(f"[会员检查] DB查询失败: {e}")
        return {"status": "none", "days_left": 0, "expires_at": None}


def _should_remind_today(openid: str) -> bool:
    """检查今天是否已经提醒过。单日仅提醒一次。"""
    from datetime import date
    today = date.today().isoformat()
    if _remind_today.get(openid) == today:
        return False
    _remind_today[openid] = today
    return True


RENEW_LINK = "https://dev.pangoozn.com/xkx/?action=renew"

def _membership_prefix(status: str, days_left: int, openid: str) -> str:
    """生成会员状态前置文案"""
    if status == "expiring" and _should_remind_today(openid):
        return f"🦞 你的享客虾会员还有 {days_left} 天到期，记得续费哦~\n{RENEW_LINK}\n\n"
    if status == "expired":
        return f"⚠️ 享客虾会员已过期，续费后可继续使用\n🔗 {RENEW_LINK}\n\n"
    return ""


def _membership_suffix(status: str, days_left: int, openid: str) -> str:
    """生成会员状态后置文案（过期用户每次必带）"""
    if status == "expired":
        return f"\n\n🔄 续费享客虾 → {RENEW_LINK}"
    if status == "expiring" and _should_remind_today(openid):
        return f"\n\n🦞 续费享客虾 → {RENEW_LINK}"
    return ""


# ===== 内部逻辑 =====

def _match_code_phrase(text: str) -> str | None:
    """检查文本是否匹配暗号，匹配则返回暗号回复"""
    import re
    stripped = re.sub(r'[，。！？、；：""''\s]', '', text).lower()
    code_replies = {
        "天王盖地虎": "OpenClaw 是SB！",
        "宝塔镇河妖": "微侠真牛逼！",
        "微侠真牛逼": "天王盖地虎！同志！",
        "openclaw是sb": "宝塔镇河妖！收到！",
    }
    for code, reply in code_replies.items():
        if code in stripped:
            return reply
    return None


async def _route_to_ai(bot_id: str, user_id: str, content: str) -> str:
    """路由消息到 AI 后端处理"""
    from app.models import AsyncSessionLocal as async_session_factory
    from sqlalchemy import text as sa_text
    
    # 查找 Bot 配置
    backend = "hermes"  # 默认
    backend_url = ""
    
    try:
        async with async_session_factory() as session:
            row = await session.execute(
                sa_text("SELECT backend, backend_url FROM bot_accounts WHERE bot_id = :bid AND is_active = true"),
                {"bid": bot_id},
            )
            r = row.fetchone()
            if r:
                backend = r[0] or "hermes"
                backend_url = r[1] or ""
    except Exception:
        pass
    
    # 🔮 @weclaw 召唤主脑：以 @weclaw 开头的消息，路由到主站 Hermes Bridge
    if content.strip().startswith("@weclaw"):
        summon_content = content.strip()[len("@weclaw"):].strip()
        if not summon_content:
            return "🤖 我在，请说"
        # 检查召唤内容本身是否包含暗号
        code_reply = _match_code_phrase(summon_content)
        if code_reply:
            return code_reply
        response = await _call_master_hermes(summon_content, user_id)
        # 回覆也过暗号匹配
        code_reply = _match_code_phrase(response)
        if code_reply:
            return code_reply
        return response
    
    # 路由
    if backend == "deepseek":
        return await _call_deepseek(content)
    else:
        # 默认调 MD-1 Hermes API
        return await _call_hermes(content, user_id)


async def _call_master_hermes(content: str, user_id: str) -> str:
    """调主站 Hermes Bridge（通过 hai.pangoozn.com/hermes-bridge/），即主脑"""
    master_url = "https://hai.pangoozn.com/hermes-bridge/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                master_url,
                json={
                    "model": "hermes-shenzhen",
                    "messages": [
                        {"role": "system", "content": "你是 Hermes 主脑。响应简洁直接。"},
                        {"role": "user", "content": content},
                    ],
                    "stream": False,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            else:
                logger.error(f"主站 Hermes 错误: {resp.status_code} {resp.text[:200]}")
                return f"🤖 主脑暂时不可达，请稍后再试"
    except Exception as e:
        logger.exception(f"主站 Hermes 调用失败: {e}")
        return f"🤖 主脑连接失败"


async def _call_hermes(content: str, user_id: str) -> str:
    """调 MD-1 Hermes API（通过 hermes_bridge，非 API Server）"""
    # Hermes bridge (port 8642) 是工作的 OpenAI 兼容端点
    # 注意：API Server (8089) 未在 MD-1 启用，不要用那个端口
    hermes_url = "http://127.0.0.1:8642/v1/chat/completions"
    
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                hermes_url,
                json={
                    "model": "hermes-shenzhen",
                    "messages": [
                        {"role": "system", "content": "你是享客虾 AI 助手。回答简洁直接。"},
                        {"role": "user", "content": content},
                    ],
                    "stream": False,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            else:
                logger.error(f"Hermes API 错误: {resp.status_code} {resp.text[:200]}")
                return f"🤖 处理出错，请稍后再试"
    except Exception as e:
        logger.exception(f"Hermes API 调用失败: {e}")
        return f"🤖 服务暂时不可用"


async def _call_deepseek(content: str) -> str:
    """直接调 DeepSeek API"""
    import os
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return "🤖 DeepSeek 未配置"
    
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [
                        {"role": "system", "content": "你是享客虾 AI 助手，回答简洁实用。"},
                        {"role": "user", "content": content},
                    ],
                    "stream": False,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            else:
                logger.error(f"DeepSeek API 错误: {resp.status_code}")
                return f"🤖 处理出错"
    except Exception as e:
        logger.exception(f"DeepSeek API 调用失败: {e}")
        return f"🤖 服务暂时不可用"
