"""
享客虾 Bot 网关 — iLink 微信机器人统一管理

架构:
  用户微信 → iLink Bot → Hermes(连接层) → 享客虾 webhook(大脑)

每个 Bot 在 Hermes 上维护 WebSocket 连接,
消息通过 webhook 转发到享客虾进行处理。
"""

import os
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import asyncio
import logging
import json
import sys
import time
import httpx

logger = logging.getLogger(__name__)

# 确保日志输出到 stderr（uvicorn 会捕获 stderr 到 journalctl）
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# ── 服务重启标记 ──
# 每次进程启动时写入时间戳,用于检测崩溃恢复后的首次请求
_RESTART_MARKER_PATH = os.path.expanduser("~/.hermes/bot_connectors/restart_marker")
try:
    os.makedirs(os.path.dirname(_RESTART_MARKER_PATH), exist_ok=True)
    with open(_RESTART_MARKER_PATH, "w") as f:
        f.write(str(time.time()))
    logger.info("[Session] 服务启动, 写入重启标记: %s", _RESTART_MARKER_PATH)
except Exception as e:
    logger.warning("[Session] 无法写入重启标记: %s", e)

router = APIRouter(prefix="/api/bot", tags=["Bot 网关"])

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

# ═══════════════════════════════════════════════════════════════
# 手机短信验证
# ═══════════════════════════════════════════════════════════════
SMS_ACCESS_KEY_ID = os.getenv("SMS_ACCESS_KEY_ID", "ALIBABA_ACCESS_KEY_ID")
SMS_ACCESS_KEY_SECRET = os.getenv("SMS_ACCESS_KEY_SECRET", "ALIBABA_ACCESS_KEY_SECRET")
SMS_SIGN_NAME = os.getenv("SMS_SIGN_NAME", "中赢智享教育科技深圳")
SMS_TEMPLATE_CODE = os.getenv("SMS_TEMPLATE_CODE", "SMS_506140441")

# 验证码存储: phone → {"code": str, "expires_at": float, "user_account_id": int}
_verify_codes: dict = {}
CODE_TTL = 300  # 5 分钟有效

# 等待绑定手机号的用户: channel_user_id → True(标记在等手机号输入)
_pending_phone: dict = {}

import random
import json as _json

def _do_phone_bind(phone: str, user_id: str, openid: str, nickname: str, user_account_id):
    """异步执行手机号绑定"""
    import asyncio
    try:
        from app.models import AsyncSessionLocal as _asf
        from sqlalchemy import text as _t
        async def _bind():
            async with _asf() as _s:
                row = await _s.execute(_t("SELECT id FROM user_accounts WHERE phone = :p"), {"p": phone})
                existing = row.fetchone()
                if existing:
                    acct_id = existing[0]
                else:
                    r2 = await _s.execute(_t("INSERT INTO user_accounts (phone, nickname) VALUES (:p, :n) RETURNING id"),
                        {"p": phone, "n": nickname or "用户"})
                    acct_id = r2.fetchone()[0]
                await _s.execute(_t("UPDATE channel_bindings SET user_account_id = :uid WHERE channel_user_id = :cuid"),
                    {"uid": acct_id, "cuid": user_id})
                await _s.execute(_t("UPDATE conversation_messages SET user_account_id = :uid WHERE openid = :oid AND user_account_id IS NULL"),
                    {"uid": acct_id, "oid": openid})
                await _s.commit()
                logger.info(f"[SMS] 手机 {phone} 绑定到用户账号 {acct_id}")
        asyncio.create_task(_bind())
    except Exception as e:
        logger.error(f"[SMS] 绑定失败: {e}")


def _send_sms_code(phone: str, code: str) -> bool:
    """使用阿里云发送短信验证码"""
    if SMS_MOCK:
        logger = logging.getLogger("weclawd.gateway")
        logger.info(f"[SMS MOCK] 验证码 {code} 发送至 {phone}")
        return True
    try:
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkdysmsapi.request.v20170525 import SendSmsRequest
        client = AcsClient(SMS_ACCESS_KEY_ID, SMS_ACCESS_KEY_SECRET, "cn-hangzhou")
        req = SendSmsRequest.SendSmsRequest()
        req.set_PhoneNumbers(phone)
        req.set_SignName(SMS_SIGN_NAME)
        req.set_TemplateCode(SMS_TEMPLATE_CODE)
        req.set_TemplateParam(_json.dumps({"code": code}))
        resp = client.do_action_with_exception(req)
        resp_data = _json.loads(resp)
        return resp_data.get("Code") == "OK"
    except Exception as e:
        logger = logging.getLogger("weclawd.gateway")
        logger.error(f"[SMS] 发送失败: {e}")
        return False

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
        return HTMLResponse("<h3>❌ 链接已失效,请重新扫码绑定</h3>", status_code=404)
    channel_type = info["channel_type"]
    channel_user_id = info["channel_user_id"]
    # 跳转到 hai.html,用户手动点绑定
    import urllib.parse
    from fastapi.responses import RedirectResponse
    bind_param = f"{channel_type}:{channel_user_id}"
    redirect_url = f"https://hai.pangoozn.com/static/bind.html?bind={urllib.parse.quote(bind_param)}"
    return RedirectResponse(url=redirect_url)

@router.post("/bind")
async def bind_channel(data: dict):
    """外部调用的通道绑定(OAuth 回调后写入)"""
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
                    sa_text("UPDATE channel_bindings SET openid = :oid, nickname = :nick, welcomed = false, bound_at = NOW() WHERE channel_type = :ct AND channel_user_id = :cuid"),
                    {"oid": openid, "nick": nickname, "ct": channel_type, "cuid": channel_user_id},
                )
            else:
                await session.execute(
                    sa_text("""INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname, welcomed, bound_at)
                        VALUES (:ct, :cuid, :oid, :nick, false, NOW())"""),
                    {"ct": channel_type, "cuid": channel_user_id, "oid": openid, "nick": nickname},
                )
            await session.commit()
        # 绑定完成后,异步推送欢迎消息到 iLink
        asyncio.create_task(_send_bind_welcome(channel_user_id, nickname))
        
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



async def _send_bind_welcome(channel_user_id: str, nickname: str):
    """绑定完成后,通过 iLink API 推送欢迎消息"""
    import httpx
    try:
        from app.models import AsyncSessionLocal as _sf
        from sqlalchemy import text as _t
        async with _sf() as _s:
            row = await _s.execute(
                _t("SELECT bot_token FROM bot_accounts WHERE is_active = true LIMIT 1")
            )
            r = row.fetchone()
            if not r:
                logger.warning(f"[绑定欢迎] 无可用 bot_token")
                return
            bot_token = r[0]
        
        text_content = (
            "✨ 欢迎加入享客虾！\n\nBot 已就绪 ✅ 发送消息即可开始对话\n\n"
            "🎯 你可以：\n  💬 随意聊天、提问\n  🎵 说「写首歌」生成 AI 音乐\n"
            "  📸 发照片制作 MV\n  🎤 声音克隆（RVC）\n\n"
            "💎 开通会员解锁全部功能：\n  https://ai.pangoozn.com/static/subscribe.html"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                json={
                    "base_info": {"channel_version": "2.2.0"},
                    "to_user_id": channel_user_id,
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": text_content}}],
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + bot_token,
                }
            )
        logger.info(f"[绑定欢迎] 已发送给 {channel_user_id[:20]}... ({nickname})")

        async with httpx.AsyncClient(timeout=10) as _c:
            await _c.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                json={
                    "base_info": {"channel_version": "2.2.0"},
                    "to_user_id": channel_user_id,
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": text_content}}],
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {bot_token}",
                }
            )
        logger.info(f"[绑定欢迎] 已发送给 {channel_user_id[:20]}... ({nickname})")
    except Exception as e:
        logger.warning(f"[绑定欢迎] 发送失败: {e}")


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




@router.patch("/{bot_id}/nickname")
async def update_bot_nickname(bot_id: str, data: dict):
    """修改 Bot 显示昵称"""
    from app.models import AsyncSessionLocal as async_session_factory
    from sqlalchemy import text as sa_text
    
    nickname = (data.get("nickname") or "").strip()
    if not nickname:
        return {"success": False, "error": "昵称不能为空"}
    
    try:
        async with async_session_factory() as session:
            await session.execute(
                sa_text("UPDATE bot_accounts SET nickname = :nick, updated_at = NOW() WHERE bot_id = :bid"),
                {"nick": nickname[:50], "bid": bot_id},
            )
            await session.commit()
            return {"success": True, "bot_id": bot_id, "nickname": nickname[:50]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/check-bound")
async def check_bound(openid: str = ""):
    """检查 openid 是否已绑定 Bot"""
    if not openid:
        return {"bound": False, "error": "缺少 openid"}
    try:
        from app.models import AsyncSessionLocal as sess
        from sqlalchemy import text as sa_text
        async with sess() as session:
            row = await session.execute(
                sa_text("SELECT channel_type, channel_user_id, nickname FROM channel_bindings WHERE openid = :o AND is_active=true LIMIT 1"),
                {"o": openid},
            )
            r = row.fetchone()
            if r:
                return {"bound": True, "channel_type": r[0], "channel_user_id": r[1], "nickname": r[2]}
    except Exception as e:
        return {"bound": False, "error": str(e)}
    return {"bound": False}

@router.post("/mock-subscribe")
async def mock_subscribe(data: dict):
    """模拟开通 - 返回二维码"""
    try:
        qr = await _fetch_qrcode()
        if not qr:
            return {"success": False, "error": "生成二维码失败"}
        qv = qr["qrcode_value"]
        lu = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={qv}&bot_type=3"
        return {"success": True, "message": "开通成功！", "qrcode_url": lu, "qrcode_value": qv, "bot_alive": False, "bot_id": data.get("bot_id", ""), "liteapp_url": lu}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, detail=f"生成二维码失败: {e}")


@router.post("/sync-subscriber")
async def sync_subscriber(data: dict):
    """同步订阅状态到 Bot 网关数据库（从 xkx 服务订阅完成后调用）"""
    try:
        from sqlalchemy import text as _t
        import logging
        logger = logging.getLogger(__name__)
        from sqlalchemy import create_engine
        engine = create_engine("postgresql://lucky:lucky_pass@localhost:5432/weclawd")

        openid = data.get("openid", "")
        plan_id = data.get("plan_id", 0)
        plan_name = data.get("plan_name", "")
        nickname = data.get("nickname", "虾友")
        messages_limit = data.get("messages_limit", 500)
        days = data.get("days", 30)

        if not openid:
            return {"success": False, "error": "缺少 openid"}

        from datetime import datetime, date, timedelta
        today = date.today()
        expires = today + timedelta(days=days)

        with engine.begin() as conn:
            # 查是否已有记录
            row = conn.execute(
                _t("SELECT id, status, expires_at FROM subscribers WHERE openid = :uid"),
                {"uid": openid},
            ).fetchone()

            if row:
                # 更新现有记录
                conn.execute(
                    _t("""UPDATE subscribers SET
                        plan_id = :pid, messages_limit = :ml, status = 'ACTIVE',
                        messages_used = 0, expires_at = :exp
                        WHERE openid = :uid"""),
                    {"pid": plan_id, "ml": messages_limit, "exp": expires,
                     "uid": openid},
                )
            else:
                # 插入新记录
                conn.execute(
                    _t("""INSERT INTO subscribers
                        (openid, nickname, plan_id, status, started_at, expires_at,
                         messages_limit, messages_used, created_at)
                        VALUES (:uid, :nick, :pid, 'ACTIVE', :now, :exp, :ml, 0, :now)"""),
                    {"uid": openid, "nick": nickname, "pid": plan_id,
                     "now": datetime.now(), "exp": expires, "ml": messages_limit},
                )

        return {"success": True, "openid": openid, "expires": expires.isoformat()}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
async def qrcode_image(value: str = None, data: str = None):
    """返回二维码图片\n
    如果不传 value,生成新的;传 value 则用指定的值生成图片
    """
    import qrcode as qrlib
    from io import BytesIO
    from fastapi.responses import Response

    if data:
        qr_data = data
    elif value:
        qr_data = value
    else:
        qr = await _fetch_qrcode()
        if not qr:
            return JSONResponse({"success": False, "error": "生成失败"}, status_code=502)
        qr_data = qr.get("qrcode_url") or qr.get("liteapp_url")
        if not qr_data:
            return JSONResponse({"success": False, "error": "二维码URL为空"}, status_code=502)

    img = qrlib.make(qr_data)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


async def _sync_activate_to_139(data: dict):
    """将激活信息同步推送到 139 服务器的 Bot 网关"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://ai.pangoozn.com/api/bot/activate",
                json=data,
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("success"):
                    logger.info(f"[139同步] Bot 激活同步成功: {data.get('bot_id','')}")
                else:
                    logger.warning(f"[139同步] 激活同步返回失败: {d.get('error','')}")
            else:
                logger.warning(f"[139同步] HTTP {r.status_code}")
    except Exception as e:
        logger.warning(f"[139同步] 请求异常: {e}")

@router.get("/connector/status")
async def check_qrcode_status(qrcode: str):
    """检查二维码扫描状态,已过期自动刷新"""
    return await _poll_single_qrcode(qrcode)


async def _fetch_qrcode() -> Optional[dict]:
    """从 iLink 获取一张新二维码(带 local_token_list 告知需要替换的旧 Bot)"""
    import httpx
    base_url = "https://ilinkai.weixin.qq.com"
    url = f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3"
    headers = {
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 4),  # OpenClaw 2.4.4
        "Content-Type": "application/json",
    }
    # 收集已有 Bot token 告知 iLink,这样扫码时才会替换旧 Bot
    local_tokens = _get_existing_bot_tokens()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json={})
            if resp.status_code != 200:
                logger.error(f"QR fetch failed: HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            qrcode_value = str(data.get("qrcode") or "")
            qrcode_url = str(data.get("qrcode_img_content") or "")
            if not qrcode_value:
                return None
            liteapp_url = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={qrcode_value}&bot_type=3"
            return {
                "qrcode_value": qrcode_value,
                "qrcode_url": liteapp_url,
                "qrcode_img": f"https://hai.pangoozn.com/api/bot/qrcode/image",
            }
    except Exception as e:
        logger.exception("生成二维码失败")
        return None


def _get_existing_bot_tokens() -> list:
    """收集所有已有 Bot 的 token,用于 QR 码请求中的 local_token_list"""
    tokens = []
    # 从 weclawd DB 读已有 bot tokens
    try:
        import sqlalchemy as sa
        engine = sa.create_engine("postgresql://lucky:lucky_pass@localhost:5432/weclawd")
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
        path = '/var/www/static/bot_qrcode.png'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.save(path)
    except Exception as e:
        logger.warning(f"保存QR图片失败: {e}")


# ===== Session 级别的二维码追踪 =====
import uuid as _uuid_lib
_qr_sessions: dict = {}  # session_id → {"qrcodes": [value1, value2, ...], "result": None or dict}

@router.get("/qrcode/session")
async def create_qr_session():
    """创建一个二维码绑定 Session，后台启动长轮询等待扫码"""
    session_id = str(_uuid_lib.uuid4()).replace("-", "")[:16]
    qr = await _fetch_qrcode()
    if not qr:
        return {"success": False, "error": "生成二维码失败"}
    _qr_sessions[session_id] = {
        "qrcodes": [qr["qrcode_value"]],
        "result": None,
    }
    await _save_qr_image(qr["qrcode_url"])
    # 后台启动长轮询（65s timeout，匹配 iLink 长轮询）
    asyncio.ensure_future(_background_qr_poll(session_id, qr["qrcode_value"]))
    return {
        "success": True,
        "session_id": session_id,
        **qr,
    }


async def _background_qr_poll(session_id: str, qrcode: str):
    """后台轮询二维码状态（Hermes 原生方案：35s 超时 + 死循环直到扫码成功）"""
    session = _qr_sessions.get(session_id)
    if not session:
        return
    
    logger.info(f"[QR] ⏳ 开始轮询 session={session_id[:12]}... qrcode={qrcode[:16]}...")
    
    import aiohttp, json as _json
    
    base_url = "https://ilinkai.weixin.qq.com"
    headers = {
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 4),
    }
    
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn) as http_session:
            deadline = time.monotonic() + 480  # 8分钟超时（匹配 Hermes）
            refresh_count = 0
            
            while time.monotonic() < deadline:
                if session.get("result"):
                    return
                try:
                    status_url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
                    timeout = aiohttp.ClientTimeout(total=35)
                    async with http_session.get(status_url, headers=headers, timeout=timeout) as resp:
                        raw = await resp.text()
                        data = _json.loads(raw)
                except asyncio.TimeoutError:
                    await asyncio.sleep(1)
                    continue
                except Exception as exc:
                    logger.warning(f"[QR] poll error: {exc}")
                    await asyncio.sleep(1)
                    continue
                
                bot_id = str(data.get("ilink_bot_id") or data.get("bot_id") or "")
                bot_token = str(data.get("bot_token") or "")
                nickname = str(data.get("nickname", ""))
                user_id = str(data.get("ilink_user_id") or data.get("user_id", ""))
                
                if bot_id and bot_token:
                    logger.info(f"[QR] ✅ 扫码成功！bot_id={bot_id} user_id={user_id}")
                    session["result"] = {
                        "success": True, "scanned": True,
                        "bot_id": bot_id, "bot_token": bot_token,
                        "nickname": nickname, "user_id": user_id,
                    }
                    # 自动激活：直接写入 DB
                    try:
                        from app.models import AsyncSessionLocal as _asf
                        from sqlalchemy import text as _st
                        async with _asf() as _db:
                            # 先查是否已有同一 user_id 的 Bot
                            old = await _db.execute(
                                _st("SELECT id FROM bot_accounts WHERE user_id = :uid AND is_active = true"),
                                {"uid": user_id},
                            )
                            if old.fetchone():
                                # 更新旧 Bot 的 token
                                await _db.execute(
                                    _st("UPDATE bot_accounts SET bot_token = :tok, is_active = true, updated_at = NOW() WHERE user_id = :uid"),
                                    {"tok": bot_token, "uid": user_id},
                                )
                                logger.info(f"[QR] 🔄 已更新旧 Bot token (user_id={user_id[:20]}...)")
                            else:
                                # 插入新 Bot
                                await _db.execute(
                                    _st("INSERT INTO bot_accounts (bot_id, bot_token, user_id, nickname, is_active, platform, backend) VALUES (:bid, :tok, :uid, :nick, true, 'weixin', 'hermes')"),
                                    {"bid": bot_id, "tok": bot_token, "uid": user_id, "nick": nickname or "虾友"},
                                )
                                logger.info(f"[QR] ✅ 新 Bot 已注册 (bot_id={bot_id})")
                            await _db.commit()
                        # 通知 keepalive 立即加载
                        try:
                            import aiohttp
                            async with aiohttp.ClientSession() as ks:
                                await ks.post("http://127.0.0.1:9100/api/reload", timeout=aiohttp.ClientTimeout(total=5))
                            logger.info(f"[QR] 📢 keepalive reload 已触发")
                        except Exception:
                            logger.warning(f"[QR] keepalive reload 不可达，30s 后自动同步")
                    except Exception as db_exc:
                        logger.warning(f"[QR] DB 激活失败: {db_exc}")
                    return
                
                status = str(data.get("status") or "wait")
                logger.info(f"[QR] status={status} bot_id={bot_id} has_token={bool(bot_token)}")
                
                if status == "scaned_but_redirect":
                    redirect_host = str(data.get("redirect_host") or "")
                    if redirect_host:
                        base_url = f"https://{redirect_host}"
                        logger.info(f"[QR] → redirect_host={redirect_host}")
                elif status == "expired":
                    refresh_count += 1
                    if refresh_count > 3:
                        logger.warning("[QR] 二维码多次过期，放弃")
                        return
                    logger.info(f"[QR] 二维码过期，刷新 ({refresh_count}/3)")
                    # 重新获取 QR
                    try:
                        async with http_session.get(
                            f"https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3",
                            headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                        ) as r:
                            raw2 = await r.text()
                            qr2 = _json.loads(raw2)
                            qrcode = str(qr2.get("qrcode") or "")
                            if qrcode:
                                session.get("qrcodes", []).append(qrcode)
                    except Exception as exc:
                        logger.warning(f"[QR] 刷新失败: {exc}")
                        return
                
                await asyncio.sleep(2)
    except Exception as exc:
        logger.error(f"[QR] critical error: {exc}")


@router.get("/qrcode/session-poll")
async def poll_qr_session(session_id: str = "", force: str = ""):
    """轮询 Session 状态（返回缓存结果 + 手动触发）
    force=1: 强制进行一次最终检查
    """
    session = _qr_sessions.get(session_id)
    if not session:
        return {"success": False, "error": "session not found"}
    
    if session["result"]:
        return session["result"]
    
    if force == "1":
        # 手动触发：对最新 QR 做一次最终检查（65s 长轮询最后一次机会）
        if session["qrcodes"]:
            from urllib.parse import quote
            last_qr = session["qrcodes"][-1]
            result = await _poll_single_qrcode(last_qr, timeout=65)
            if result.get("scanned") or result.get("partial"):
                session["result"] = result
                return result
            return {"success": True, "scanned": False, "status": "expired", "message": "请重新获取二维码"}
    
    return {"success": True, "scanned": False, "status": "wait"}


@router.post("/qrcode/claim")
async def claim_qr_session(data: dict):
    """用户手动扫码后主动 claim：检查 QR 状态 + 执行激活"""
    session_id = data.get("session_id", "")
    openid = data.get("openid", "")
    nickname = data.get("nickname", "")
    
    session = _qr_sessions.get(session_id)
    if not session:
        return {"success": False, "error": "session not found or expired"}
    
    # 如果已有扫码结果，直接激活
    if session["result"]:
        r = session["result"]
        if r.get("bot_id") and r.get("bot_token"):
            return await _do_activate(r["bot_id"], r["bot_token"], 
                                       r.get("user_id", openid), 
                                       r.get("nickname", nickname))
        if r.get("partial"):
            # 部分识别到 bot_id 但无 token → 尝试用旧 token 列表或 force claim
            return {"success": False, "error": "partial_scan", 
                    "message": "已检测到扫码，但未获取到完整的 bot 凭证，请重试",
                    "bot_id": r.get("bot_id")}
    
    # 还未检测到扫码，做一次最终检查
    if session["qrcodes"]:
        last_qr = session["qrcodes"][-1]
        result = await _poll_single_qrcode(last_qr, timeout=65)
        if result.get("scanned"):
            session["result"] = result
            if result.get("bot_id") and result.get("bot_token"):
                return await _do_activate(result["bot_id"], result["bot_token"],
                                           result.get("user_id", openid),
                                           result.get("nickname", nickname))
            if result.get("bot_id"):
                return {"success": False, "error": "partial_scan",
                        "bot_id": result.get("bot_id")}
    
    return {"success": False, "error": "not_scanned", "message": "未检测到扫码，请在微信中扫描二维码"}


async def _do_activate(bot_id: str, bot_token: str, user_id: str, nickname: str):
    """执行激活流程：调用已有 activate_bot 的注册+欢迎逻辑"""
    data = {"bot_id": bot_id, "bot_token": bot_token, 
            "user_id": user_id, "nickname": nickname}
    result = await activate_bot(data)
    return result


async def _poll_single_qrcode(qrcode: str, timeout: int = 5) -> dict:
    """单次查询二维码状态（短查用 5s，最终检查用 65s）"""
    import httpx
    base_url = "https://ilinkai.weixin.qq.com"
    headers = {
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 4),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            status_url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
            try:
                resp = await client.get(status_url, headers=headers)
                data = resp.json()
            except (httpx.TimeoutException, httpx.ReadTimeout):
                return {"success": True, "scanned": False, "status": "wait"}
            except Exception:
                return {"success": True, "scanned": False, "status": "wait"}
            
            # 先检查 bot 凭证（任何 status 都可能带）
            bot_id = str(data.get("ilink_bot_id") or data.get("bot_id") or "")
            bot_token = str(data.get("bot_token") or "")
            nick = str(data.get("nickname", ""))
            uid = str(data.get("ilink_user_id") or data.get("user_id", ""))
            
            if bot_id and bot_token:
                return {"success": True, "scanned": True, "bot_id": bot_id,
                        "bot_token": bot_token, "nickname": nick, "user_id": uid}
            
            status = str(data.get("status") or "wait")
            
            if status == "scaned_but_redirect":
                # 被扫码但无 token → partial
                return {"success": True, "scanned": True, "partial": True,
                        "status": "scaned_but_redirect", "bot_id": bot_id,
                        "nickname": nick, "user_id": uid}
            
            if status == "scaned":
                return {"success": True, "scanned": False, "status": "scaned"}
            
            if status == "confirmed" and bot_id:
                return {"success": True, "scanned": True, "bot_id": bot_id, "partial": True}
            
            if status == "expired":
                return {"success": True, "scanned": False, "expired": True,
                        "status": "expired"}
            
            return {"success": True, "scanned": False, "status": "wait"}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/activate")
async def activate_bot(data: dict):
    """扫码确认后:注册 Bot + 启动连接器"""
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
    
    # 2. 绑定通道身份(直接用 iLink user_id,不需额外 OAuth)
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
                    sa_text("""INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname, welcomed, bound_at)
                        VALUES ('ilink', :cuid, :cuid, :nick, true, NOW())"""),
                    {"cuid": user_id, "nick": nickname},
                )
                await session.commit()
                logger.info(f"[激活] 通道绑定成功: {user_id[:20]} ({nickname})")
    except Exception as e:
        logger.warning(f"[激活] 通道绑定失败(不影响使用): {e}")

    # 3. 发送欢迎消息(统一连接器 30s 内自动从 DB 接入新 Bot)
    try:
        import httpx
        welcome_text = (
            "\U0001f99e \u6b22\u8fce\u52a0\u5165\u4eab\u5ba2\u867e\uff01 \U0001f389\n\n"
            "\u626b\u7801\u6210\u529f\uff0cBot \u5df2\u5c31\u7eea \u2705\n\n"
            "\u25b6\ufe0f \u5f00\u901a\u4f1a\u5458\uff1ahttps://hai.pangoozn.com/subscribe\n\n"
            "\u5f00\u901a\u540e\u53ef\u89e3\u9501\uff1a\n"
            "\u2022 \U0001f4ac AI \u5bf9\u8bdd\n"
            "\u2022 \U0001f3b5 AI \u5199\u6b4c\n"
            "\u2022 \U0001f3ac \u7167\u7247 MV\n"
            "\u2022 \U0001f3a4 \u58f0\u97f3\u514b\u9686\n\n"
            "\u73b0\u5728\u5c31\u53bb\u5f00\u901a\uff01"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                json={
                    "base_info": {"channel_version": "2.2.0"},
                    "to_user_id": user_id,
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": welcome_text}}],
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + bot_token,
                }
            )
        logger.info("[\u6fc0\u6d3b] \u6b22\u8fce\u6d88\u606f\u5df2\u53d1\u9001: " + str(user_id[:20]))
    except Exception as e:
        logger.warning("[\u6fc0\u6d3b] \u6b22\u8fce\u6d88\u606f\u53d1\u9001\u5931\u8d25: " + str(e))

    return {"success": True, "bot_id": bot_id, "message": "Bot " + (nickname or bot_id) + " \u5df2\u6fc0\u6d3b\uff0c\u8fde\u63a5\u5668 30s \u5185\u63a5\u5165"}

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
            return {"success": False, "bot_id": bot_id, "message": "启动失败,查看日志"}
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
    
    # 走网关处理逻辑(暗号匹配 + AI 路由)
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
    """内部调用:处理消息并返回回复(含额度检查)"""
    if not content:
        return {"success": False, "error": "empty content"}
    
    import re
    stripped = re.sub(r'[,。!?、;:""''\s]', '', content).lower()
    code_replies = {
        "天王盖地虎": "OpenClaw 是SB!",
        "宝塔镇河妖": "微侠真牛逼!",
        "微侠真牛逼": "天王盖地虎!同志!",
        "openclaw是sb": "宝塔镇河妖!收到!",
    }
    for code, reply in code_replies.items():
        if code in stripped:
            logger.info(f"[暗号] bot={bot_id} matched: {code}")
            return {"success": True, "response": reply}
    
    # 额度检查 + 消耗
    from app.bot.quota import check_and_consume
    quota_result = check_and_consume(user_id)
    
    if not quota_result["ok"]:
        logger.info(f"[Quota] 用户 {user_id[:20]} 额度已用完")
        return {"success": True, "response": quota_result["message"]}
    
    # 路由到 AI
    # --- 通道内改昵称命令:发 "改昵称 xxx" ---
    import re as _re
    nick_match = _re.match(r'^改昵称\s+(.+)$', content.strip())
    if nick_match:
        new_nick = nick_match.group(1).strip()[:20]
        if new_nick:
            try:
                from app.models import AsyncSessionLocal as _asf2
                from sqlalchemy import text as _t2
                async with _asf2() as _s:
                    await _s.execute(
                        _t2("UPDATE bot_accounts SET nickname = :n, updated_at = NOW() WHERE bot_id = :bid"),
                        {"n": new_nick, "bid": bot_id},
                    )
                    await _s.commit()
                logger.info("[昵称] bot=%s 昵称改为: %s", bot_id[:20], new_nick)
                return {"success": True, "response": "✅ 昵称已改为「" + new_nick + "」"}
            except Exception as _e:
                logger.warning("[昵称] 修改失败: %s", _e)
                return {"success": True, "response": "❌ 昵称修改失败,请重试"}
        else:
            return {"success": True, "response": "❌ 昵称不能为空"}

    # --- 跨通道发消息:发 "发给昵称 消息" ---
    send_match = re.match(r'^发给\s*(\S+)\s*[::,,\s]+(.+)$', content.strip())
    if send_match:
        target_nick = send_match.group(1).strip()
        msg_content = send_match.group(2).strip()
        
        if msg_content:
            try:
                from app.models import AsyncSessionLocal as _asf_s
                from sqlalchemy import text as _st
                async with _asf_s() as _s:
                    row = await _s.execute(
                        _st("SELECT cb.channel_user_id, ba.bot_id FROM channel_bindings cb JOIN bot_accounts ba ON ba.is_active = true WHERE cb.nickname = :nick LIMIT 1"),
                        {"nick": target_nick},
                    )
                    r = row.fetchone()
                    if r:
                        target_user = r[0]
                        target_bot = r[1]
                        _sr = await _s.execute(
                            _st("SELECT nickname FROM channel_bindings WHERE channel_user_id = :uid LIMIT 1"),
                            {"uid": user_id},
                        )
                        _sn = _sr.fetchone()
                        _snick = _sn[0] if _sn else "智享家"
                        msg_content = f"来自《{_snick}》:{msg_content}"
                        await _s.execute(
                            _st("INSERT INTO push_queue (bot_id, to_user, content) VALUES (:bid, :uid, :ct)"),
                            {"bid": target_bot, "uid": target_user, "ct": msg_content},
                        )
                        await _s.commit()
                        logger.info(f"[发送] {user_id[:20]} -> {target_nick}: {msg_content[:30]}")
                        return {"success": True, "response": f"\u2705 \u5df2\u53d1\u9001\u7ed9\u300c{target_nick}\u300d"}
                    else:
                        row2 = await _s.execute(
                            _st("SELECT bot_id FROM bot_accounts WHERE nickname = :nick AND is_active = true LIMIT 1"),
                            {"nick": target_nick},
                        )
                        r2 = row2.fetchone()
                        if r2:
                            return {"success": True, "response": f"\u2757 \u627e\u5230\u4e86\u300c{target_nick}\u300d\uff0c\u4f46\u8be5\u7528\u6237\u672a\u7ed1\u5b9a\u5fae\u4fe1"}
                        return {"success": True, "response": f"\u274c \u627e\u4e0d\u5230\u7528\u6237\u300c{target_nick}\u300d"}
            except Exception as _e:
                logger.warning(f"[\u53d1\u9001] \u5931\u8d25: {_e}")
                return {"success": True, "response": "\u274c \u53d1\u9001\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5"}
        else:
            return {"success": True, "response": "\u683c\u5f0f\uff1a\u53d1\u7ed9{\u6635\u79f0} \u6d88\u606f\u5185\u5bb9"}
    
    # 查 Bot 配置的昵称(用户可自定义)
    bot_nickname = user_id
    try:
        from app.models import AsyncSessionLocal as async_session_factory
        from sqlalchemy import text as sa_text
        async with async_session_factory() as session:
            row = await session.execute(
                sa_text("SELECT nickname FROM bot_accounts WHERE bot_id = :bid AND is_active = true"),
                {"bid": bot_id},
            )
            r = row.fetchone()
            if r and r[0]:
                bot_nickname = r[0]
    except Exception:
        pass
    response = await _route_to_ai(bot_id, user_id, content, bot_nickname)
    
    # 如果是首条消息,在 AI 回复前加欢迎
    if quota_result.get("welcome"):
        response = quota_result["welcome"] + "\n\n" + response
    
    return {"success": True, "response": response}


@router.post("/webhook")
async def bot_webhook(data: dict):
    """
    Hermes 转发消息的 webhook 入口。
    
    Hermes 收到微信消息后,POST 到此端点:
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
    media_path = data.get("media_path", "")
    
    if not content and not media_path:
        return {"success": False, "error": "empty content"}

    # 暗号匹配(所有 Bot 通用)
    import re
    stripped = re.sub(r'[,。!?、;:""''\s]', '', content).lower()
    code_replies = {
        "天王盖地虎": "OpenClaw 是SB!",
        "宝塔镇河妖": "微侠真牛逼!",
        "微侠真牛逼": "天王盖地虎!同志!",
        "openclaw是sb": "宝塔镇河妖!收到!",
    }
    for code, reply in code_replies.items():
        if code in stripped:
            logger.info(f"[暗号] bot={bot_id} matched: {code}")
            return {"success": True, "response": reply}

    # @weclaw 前缀 -> 剥掉当普通消息处理(主脑召唤已禁用)
    if content.strip().startswith('@weclaw'):
        content = content.replace('@weclaw', '', 1).strip()
        logger.info("[Bot Webhook] @weclaw 前缀已剥离,按普通消息处理")

    # == 通道绑定检查 ==
    # 检测用户是否已绑定(ilink 通道的 user_id 即为 channel_user_id)
    channel_type = "ilink"
    channel_user_id = user_id
    bound_info = await _check_binding(channel_type, channel_user_id)
    
    if not bound_info.get("bound"):
        # 未绑定 → 自动绑定(用户已扫码,只是激活回调没到)
        try:
            from app.models import AsyncSessionLocal as _asf
            from sqlalchemy import text as _t
            async with _asf() as _s:
                await _s.execute(
                    _t("""INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname, welcomed, bound_at)
                        VALUES (:ct, :cuid, :cuid, '', true, NOW())
                        ON CONFLICT (channel_type, channel_user_id) DO NOTHING"""),
                    {"ct": channel_type, "cuid": channel_user_id},
                )
                await _s.commit()
            logger.info(f"[自动绑定] 用户 {channel_user_id[:20]} 已自动绑定")
        except Exception as e:
            logger.warning(f"[自动绑定] 失败: {e}")
        # 发送欢迎消息 + 开通页面链接
        response = ("\U0001f99e \u6b22\u8fce\u52a0\u5165\u4eab\u5ba2\u867e\uff01 \U0001f389\n\n"
                    "\u70b9\u51fb\u67e5\u770b\u6b22\u8fce\u9875\uff1a\n"
                    "https://hai.pangoozn.com/subscribe")
        return {"success": True, "response": response}
    
    # 已绑定 → 注入用户身份
    openid = bound_info.get("openid", "")
    nickname = bound_info.get("nickname", "")
    welcomed = bound_info.get("welcomed", True)
    user_account_id = bound_info.get("user_account_id")

    # 首次绑定欢迎
    if not welcomed:
        from app.models import AsyncSessionLocal as _asf
        from sqlalchemy import text as _t
        async with _asf() as _s:
            await _s.execute(
                _t("UPDATE channel_bindings SET welcomed = true WHERE channel_type = :ct AND channel_user_id = :cuid"),
                {"ct": "ilink", "cuid": user_id},
            )
            await _s.commit()
        response = f"\U0001f99e \u6b22\u8fce\u52a0\u5165\u4eab\u5ba2\u867e\uff01{nickname} \U0001f389\n\n\u25b6\ufe0f \u5f00\u901a\u4f1a\u5458\uff1ahttps://hai.pangoozn.com/subscribe\n\n\u5f00\u901a\u540e\u53ef\u89e3\u9501\uff1a\n\u2022 \U0001f4ac AI \u5bf9\u8bdd\n\u2022 \U0001f3b5 AI \u5199\u6b4c\n\u2022 \U0001f3ac \u7167\u7247 MV\n\u2022 \U0001f3a4 \u58f0\u97f3\u514b\u9686"
        return {"success": True, "response": response}

    if nickname:
        logger.info(f"[Bot Webhook] 已识别用户: {nickname} (openid={openid[:15]}...)")

    # ═══════════════════════════════════════════════════════════════
    # 手机绑定流程
    # ═══════════════════════════════════════════════════════════════
    stripped_phone = re.sub(r'[,。!?、;:""''\s]', '', content).lower()
    
    # 正在等验证码
    if _pending_phone.get(user_id):
        # 5分钟超时自动清除
        if isinstance(_pending_phone[user_id], (int, float)) and time.time() - _pending_phone[user_id] > 300:
            _pending_phone.pop(user_id, None)
            logger.info(f"[SMS] 验证码等待超时,已清除 user_id={user_id[:20]}")
        else:
            if re.match(r'^\d{4,6}$', stripped_phone):
                # 测试模式: 666666 万能验证码
                if SMS_MOCK and stripped_phone == "666666":
                    phone = ""
                    for _c, _d in list(_verify_codes.items()):
                        if isinstance(_d, dict) and _d.get("phone"):
                            phone = _d["phone"]
                            break
                    if phone:
                        _do_phone_bind(phone, user_id, openid, nickname, user_account_id)
                        _pending_phone.pop(user_id, None)
                        return {"success": True, "response": "\u2705 手机号 {phone} 绑定成功!(测试模式)"}
                    return {"success": True, "response": "\u274c 请先发 绑定手机138xxxx 获取验证码"}

                code_data = _verify_codes.get(stripped_phone)
                if code_data and code_data["expires_at"] > time.time():
                    phone = code_data["phone"]
                    _do_phone_bind(phone, user_id, openid, nickname, user_account_id)
                    del _verify_codes[stripped_phone]
                    _pending_phone.pop(user_id, None)
                    return {"success": True, "response": f"✅ 手机号 {phone} 绑定成功!以后你用这个手机号的任何微信号都能共享同一个对话。"}
                else:
                    return {"success": True, "response": "❌ 验证码错误或已过期,输入 绑定手机 重新获取。"}
            else:
                return {"success": True, "response": "请回复收到的6位验证码,或等待超时后自动取消。"}
    
    # "绑定手机138xxx" 指令
    m = re.match(r'^绑定手机\s*(\d{11})$', stripped_phone)
    if m:
        phone = m.group(1)
        code = str(random.randint(100000, 999999))
        _verify_codes[code] = {"phone": phone, "expires_at": time.time() + CODE_TTL}
        _pending_phone[user_id] = time.time()
        if _send_sms_code(phone, code):
            logger.info(f"[SMS] 验证码已发送到 {phone}")
            return {"success": True, "response": f"📱 验证码已发送到 {phone[:3]}****{phone[-4:]},请回复6位验证码完成绑定。"}
        else:
            return {"success": True, "response": "❌ 短信发送失败,请稍后再试。"}
    
    # --- 跨通道发消息:发 "发给昵称 消息" ---
    import re as _re_send
    send_match = _re_send.match(r'^发给\s*(\S+)\s*[::,,\s]+(.+)$', content.strip())
    if send_match:
        target_nick = send_match.group(1).strip()
        msg_content = send_match.group(2).strip()
        if msg_content:
            try:
                from app.models import AsyncSessionLocal as _asf_s
                from sqlalchemy import text as _st
                async with _asf_s() as _s:
                    row = await _s.execute(
                        _st("SELECT cb.channel_user_id, ba.bot_id FROM channel_bindings cb JOIN bot_accounts ba ON ba.is_active = true WHERE cb.nickname = :nick LIMIT 1"),
                        {"nick": target_nick},
                    )
                    r = row.fetchone()
                    if r:
                        target_user = r[0]
                        target_bot = r[1]
                        await _s.execute(
                            _st("INSERT INTO push_queue (bot_id, to_user, content) VALUES (:bid, :uid, :ct)"),
                            {"bid": target_bot, "uid": target_user, "ct": msg_content},
                        )
                        await _s.commit()
                        return {"success": True, "response": "✅ 已发送给「" + target_nick + "」"}
                    else:
                        row2 = await _s.execute(
                            _st("SELECT bot_id FROM bot_accounts WHERE nickname = :nick AND is_active = true LIMIT 1"),
                            {"nick": target_nick},
                        )
                        r2 = row2.fetchone()
                        if r2:
                            return {"success": True, "response": "❗ 找到了「" + target_nick + "」,但该用户未绑定微信"}
                        return {"success": True, "response": "❌ 找不到用户「" + target_nick + "」"}
            except Exception as _e:
                logger.warning("[发送] 失败: %s", str(_e)[:200])
                return {"success": True, "response": "❌ 发送失败,请重试"}
        else:
            return {"success": True, "response": "格式:发给{昵称} 消息内容"}
    
    # 路由到对应的 AI 后端
    response = await _route_to_ai(bot_id, user_id, content, nickname, openid, user_account_id, media_path)
    return {"success": True, "response": response}


async def _check_binding(channel_type: str, channel_user_id: str) -> dict:
    """检查通道绑定状态(直接查 DB,不走 HTTP 避免死锁)"""
    try:
        from app.models import AsyncSessionLocal as async_session_factory
        from sqlalchemy import text as sa_text
        async with async_session_factory() as session:
            row = await session.execute(
                sa_text("SELECT openid, nickname, welcomed, user_account_id FROM channel_bindings WHERE channel_type = :ct AND channel_user_id = :cuid"),
                {"ct": channel_type, "cuid": channel_user_id},
            )
            r = row.fetchone()
            if r:
                return {"bound": True, "openid": r[0], "nickname": r[1] or "", "welcomed": r[2], "user_account_id": r[3]}
    except Exception as e:
        logger.warning(f"[绑定检查] DB 查询失败: {e}")
    return {"bound": False}


# ===== 内部逻辑 =====

async def _route_to_ai(bot_id: str, user_id: str, content: str, user_nickname: str = "", openid: str = "", user_account_id: int = None, media_path: str = "") -> str:
    """路由消息到 AI 后端处理"""
    # L0: 暗号匹配 → 直接回复，零成本
    code_reply = match_code_phrase(content)
    if code_reply:
        return code_reply
    
    # ── 工具集路由 ──
    stripped = content.strip()
    
    # 八字排盘
    import re as _re_bazi
    bazi_match = _re_bazi.search(r'(?:八字|排盘|生辰|生日)(.+?)(?:排盘|八字)?$', stripped)
    if bazi_match or any(kw in stripped for kw in ["排盘", "我的八字"]):
        if any(kw in stripped for kw in ["解读", "解析", "分析", "深读"]):
            pass  # 交给 AI 做深度解读
        else:
            try:
                from app.bot.bazi_tool import get_bazi, format_bazi_result, format_hepan
                # 解析日期：支持 YYYY-MM-DD HH 或 YYYY年MM月DD日 HH时
                date_pattern = _re_bazi.search(r'(\d{4})[年\-/](\d{1,2})[月\-/]?(\d{1,2})[日]?(?:\s+(\d{1,2}))?[时]?', stripped)
                if date_pattern:
                    y, m, d = int(date_pattern.group(1)), int(date_pattern.group(2)), int(date_pattern.group(3))
                    h = int(date_pattern.group(4)) if date_pattern.group(4) else 12
                    gender = "男" if "男" in stripped else ("女" if "女" in stripped else "未知")
                    result = get_bazi(y, m, d, h, gender)
                    return format_bazi_result(result)
                # 合盘检测：两个日期
                date_pairs = _re_bazi.findall(r'(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})', stripped)
                if len(date_pairs) >= 2 and ("合" in stripped or "婚" in stripped or "配" in stripped):
                    y1,m1,d1 = int(date_pairs[0][0]), int(date_pairs[0][1]), int(date_pairs[0][2])
                    y2,m2,d2 = int(date_pairs[1][0]), int(date_pairs[1][1]), int(date_pairs[1][2])
                    g1 = "男" if "男" in stripped[:stripped.find(str(y1))] else ("女" if "女" in stripped else "A")
                    g2 = "男" if "男" in stripped[stripped.find(str(y1)):] else ("女" if "女" in stripped else "B")
                    p1 = get_bazi(y1,m1,d1,12,g1)
                    p2 = get_bazi(y2,m2,d2,12,g2)
                    return format_hepan(p1, p2)
                return ("输入格式示例：\n• 单排：1990年5月15日 12时 男\n"
                        "• 合盘：1990-05-15 女 和 1988-08-20 男\n"
                        "• 合盘解读：以上日期后加上「解读」")
            except Exception as e:
                logger.warning("[八字] 排盘失败: %s", str(e)[:100])
                return "❌ 排盘失败，请检查日期格式，示例：1990年5月15日 12时 男"
    
    # 量化查询
    if any(kw in stripped for kw in ["量化", "信号", "行情", "今天A股", "今天美股", "四市", "大盘"]):
        try:
            from app.bot.quant_tool import format_quant_summary
            return format_quant_summary()
        except Exception as e:
            logger.warning("[量化] 查询失败: %s", str(e)[:100])
            return "❌ 量化数据查询失败"
    
    # 详细信号（量化数据展开）
    if any(kw in stripped for kw in ["详细信号", "详细量化", "详细数据"]):
        try:
            from app.bot.quant_tool import format_crypto_detail
            return format_crypto_detail()
        except Exception as e:
            pass
    
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
    
    # 路由
    if backend == "deepseek":
        return await _call_deepseek(content, user_nickname)
    else:
        # 默认调 MD-1 Hermes API
        return await _call_hermes(content, user_id, user_nickname, openid, user_account_id, media_path, bot_id)



# 信号量控制:最多 2 个并发 Hermes 请求,防止超时积压
_hermes_semaphore = None

def _get_hermes_semaphore():
    global _hermes_semaphore
    if _hermes_semaphore is None:
        import asyncio
        _hermes_semaphore = asyncio.Semaphore(5)
    return _hermes_semaphore


_hermes_client = None


def _get_hermes_client():
    """复用连接池，避免每次新建 TCP 连接"""
    import httpx
    global _hermes_client
    if _hermes_client is None:
        _hermes_client = httpx.AsyncClient(
            timeout=300,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _hermes_client


OPENROUTER_API_KEY = "sk-or-v1-ALIBABA_ACCESS_KEY_SECRET"
OPENROUTER_VISION_MODEL = "qwen/qwen2.5-vl-72b-instruct"
VISION_TIMEOUT = 30

async def _describe_image(media_path: str) -> str:
    """调用 OpenRouter 视觉模型识别图片内容,返回文字描述"""
    import base64
    try:
        with open(media_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        payload = {
            "model": OPENROUTER_VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "用一句话简洁描述这张图片的内容,包括文字和关键视觉元素。如果图片是文字截图,请提取其中的关键信息。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            }],
            "max_tokens": 300
        }
        async with httpx.AsyncClient(timeout=VISION_TIMEOUT) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json"
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            else:
                return f"[图片识别失败: HTTP {resp.status_code}]"
    except Exception as e:
        return f"[图片识别异常: {e}]"



# ═══════════════════════════════════════════════════
# 消息分级调度 — 控制上下文量，节省 API 成本
# ═══════════════════════════════════════════════════

import re

_CODE_PHRASES = {
    "天王盖地虎": "OpenClaw 是SB！",
    "宝塔镇河妖": "微侠真牛逼！",
    "微侠真牛逼": "天王盖地虎！同志！",
    "openclaw是sb": "宝塔镇河妖！收到！",
    "openclaw is sx": "宝塔镇河妖！收到！",
}

def match_code_phrase(text: str) -> str:
    """匹配暗号，命中直接返回回复（不调AI）"""
    cleaned = re.sub(r'[，。！？、；：""''\s]', '', text).lower()
    for phrase, reply in _CODE_PHRASES.items():
        if phrase.lower() in cleaned:
            return reply
    return ""


def classify_message_level(content: str) -> int:
    """判断消息级别: 0=暗号, 1=轻量, 2=常规, 3=深度"""
    if not content or not content.strip():
        return 1
    
    text = content.strip()
    
    # L1: 轻量 — 单字/短问候/状态查询
    l1_patterns = [
        r'^(hi|hello|嗨|你好|在吗|在不在|测试|test|ping)$',
        r'^(在|好|嗯|哦|行|ok|okay|好的|收到|知道|来了)$',
        r'^(查|余额|状态|几点了|天气|日期|时间)$',
        r'^[?？!！.。]+$',
    ]
    for p in l1_patterns:
        if re.match(p, text, re.IGNORECASE):
            return 1
    
    # L3: 深度任务 — 写/分析/修改/生成/评估
    l3_keywords = [
        '写', '分析', '修改', '优化', '生成', '评估', '检查',
        'PDF', 'docx', '文档', '报告', '方案',
        '读取', '打开文件', '翻译',
    ]
    for kw in l3_keywords:
        if kw in text:
            return 3
    
    # L2: 默认（常规对话）
    return 2


# ── Hermes Bridge 熔断器 ──
class CircuitBreakerOpen(Exception):
    """熔断器打开异常"""
    pass

class _HermesCircuitBreaker:
    """熔断器：连续失败 N 次后切换到降级模式，recovery_timeout 后恢复"""
    
    def __init__(self, failure_threshold=3, recovery_timeout=120):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._last_failure_time = 0.0
        self._state = 'closed'
    
    async def call(self, coro, timeout=120):
        if self._state == 'open':
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = 'half-open'
                logger.info('[CB] 熔断器半开，允许探活')
            else:
                raise CircuitBreakerOpen('Hermes 熔断中')
        
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            self._failures = max(0, self._failures - 1)
            if self._state == 'half-open':
                self._state = 'closed'
                self._failures = 0
                logger.info('[CB] 熔断器恢复关闭')
            return result
        except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException) as e:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._failures >= self.failure_threshold:
                self._state = 'open'
                logger.warning(f'[CB] 熔断器打开 (连续{self._failures}次失败)')
            raise
        except Exception as e:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._failures >= self.failure_threshold:
                self._state = 'open'
                logger.warning(f'[CB] 熔断器打开 (异常:{e})')
            raise

_hermes_cb = _HermesCircuitBreaker(failure_threshold=3, recovery_timeout=120)

# 消息级别对应的超时
_HERMES_TIMEOUTS = {1: 60, 2: 300, 3: 600}


# ── 会话上下文保全工具 ──
async def _save_conversation_pair(session_key, user_account_id, openid, user_content, assistant_content, _asf=None, _t=None):
    """保存一对对话（用户消息 + 助手回复）到 conversation_messages 表
    
    确保无论走 Hermes 还是降级 DeepSeek，对话都被记录，
    下次请求时 Hermes 能读到完整上下文。
    """
    if not session_key:
        return
    if _asf is None:
        from app.models import AsyncSessionLocal as _asf
    if _t is None:
        from sqlalchemy import text as _t
    try:
        async with _asf() as _s:
            for role, msg_content in [
                ("user", user_content),
                ("assistant", assistant_content),
            ]:
                if user_account_id:
                    await _s.execute(
                        _t("INSERT INTO conversation_messages (user_account_id, role, content, openid) VALUES (:uid, :role, :content, :oid)"),
                        {"uid": user_account_id, "role": role, "content": msg_content, "oid": openid or ""},
                    )
                else:
                    await _s.execute(
                        _t("INSERT INTO conversation_messages (openid, role, content) VALUES (:oid, :role, :content)"),
                        {"oid": openid, "role": role, "content": msg_content},
                    )
            await _s.commit()
    except Exception as e:
        logger.warning(f"[Session] 保存历史失败: {e}")


def _detect_restart_gap() -> str:
    """检测服务是否经历过重启，返回间隔描述或空字符串"""
    try:
        if not os.path.exists(_RESTART_MARKER_PATH):
            return ""
        with open(_RESTART_MARKER_PATH) as f:
            marker = f.read().strip()
        restart_time = float(marker)
        now = time.time()
        elapsed = now - restart_time
        # 重启后 10 分钟内算"刚恢复"
        if elapsed < 600:
            gap_sec = int(elapsed)
            if gap_sec < 60:
                return f"我刚完成资源重配（约{max(1, gap_sec)}秒前）"
            else:
                return f"我刚完成资源重配（约{max(1, gap_sec//60)}分钟前）"
    except Exception:
        pass
    return ""


async def _call_hermes(content: str, user_id: str, user_nickname: str = "", openid: str = "", user_account_id: int = None, media_path: str = "", bot_id: str = "") -> str:
    """调 MD-1 Hermes API"""
    from app.models import AsyncSessionLocal as _asf
    from sqlalchemy import text as _t
    # 从 DB 读取近期对话历史用作上下文
    history_messages = []

    # 构建 system prompt(含用户信息)
    user_info_parts = []
    if user_nickname:
        user_info_parts.append(f"当前用户昵称: {user_nickname}")
    if openid:
        user_info_parts.append(f"微信 OpenID: {openid[:12]}...")
    user_info = " | ".join(user_info_parts) if user_info_parts else "未知用户"
    # 图片/文件消息: content为空或媒体占位符时注入图片描述
    if content.strip() in ("[图片]", "[视频]", "[语音]", "[文件]"):
        content = ""
    if media_path and not content.strip():
        fname = os.path.basename(media_path)
        ext = os.path.splitext(fname)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            desc = await _describe_image(media_path)
            content = "[用户发来一张图片:{}]\n🖼️ 图片识别: {}".format(fname, desc)
        elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            content = "[用户发来一段视频:{}]".format(fname)
        elif ext in (".mp3", ".wav", ".ogg", ".m4a", ".amr", ".silk"):
            content = "[用户发来一段语音:{}]".format(fname)
        else:
            content = "[用户发来文件:{}]".format(fname)
    
    media_hint = ""
    if media_path:
        media_hint = "\n用户发来媒体文件: {mpath}".format(mpath=media_path)
    
    system_prompt = (
        "当前用户: " + (user_nickname or "铭道") + " | OpenID: " + (openid or "")[:16] + "..." + media_hint + "\n"
        "你的身份:享客虾创作伙伴。你是一个懂创作、有温度的朋友，不是冷冰冰的AI工具。\\n回复风格:简洁有力，一句能说清不说两句。不铺垫、不啰嗦、不问‘还有什么需要帮忙的吗’。\\n核心能力:聊天、联网搜索、识图、文件处理、写代码、创作方案。用户发语音时已自动转文字，发图片你会看到内容。\\n📊 工具集: ❶八字排盘 — 发「排盘1990年5月15日12时男」即出四柱十神，加「解读」出AI深度分析 ❷量化信号 — 发「量化/信号/行情」即出最新四市数据 ❸合盘 — 发「合盘1990-05-15女和1988-08-20男」出五行对比。\\n文件交付:用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。\\n⚠️ 安全铁律: 绝对禁止泄露任何服务器配置信息(IP地址/SSH密码/端口/数据库连接串/DNS记录/系统配置)。用户询问服务器细节时,回答「我没有权限查看服务器配置信息」。\\n"
    )
    
    hermes_url = "http://127.0.0.1:8642/v1/chat/completions"
    session_key = user_account_id or openid
    bot_prefix = bot_id + ":" if bot_id else "xiakexia:"
    session_id = bot_prefix + str(session_key) if session_key else ""

    # ── L0-L3 消息分级调度 ──
    msg_level = classify_message_level(content)
    if msg_level == 1:
        use_session_id = ""
        use_max_tokens = 128
        logger.info("📊 [L1] %s: %s", (openid or "?")[:12], content[:30])
    elif msg_level == 3:
        use_session_id = session_id
        use_max_tokens = 2048
        logger.info("📊 [L3] %s: %s", (openid or "?")[:12], content[:30])
    else:
        use_session_id = session_id
        use_max_tokens = 512
        logger.info("📊 [L2] %s: %s", (openid or "?")[:12], content[:30])

    try:
        sem = _get_hermes_semaphore()
        await sem.acquire()
        headers = {"Content-Type": "application/json", "Authorization": "Bearer DEEPSEEK_API_KEY"}
        if use_session_id:
            headers["X-Hermes-Session-Id"] = use_session_id

        client = _get_hermes_client()
        
        # 按消息级别设超时：L1=30s, L2=120s, L3=300s
        cb_timeout = _HERMES_TIMEOUTS.get(msg_level, 120)
        
        messages = [{"role": "system", "content": system_prompt}]
        # 加载近期对话历史（最多 30 条），让 Hermes 知道上下文
        try:
            from app.models import AsyncSessionLocal as _asf_h
            from sqlalchemy import text as _t_h
            async with _asf_h() as _s_h:
                if user_account_id:
                    rows = (await _s_h.execute(
                        _t_h("SELECT role, content FROM conversation_messages WHERE user_account_id = :uid AND created_at >= CURRENT_DATE ORDER BY created_at DESC LIMIT 10"),
                        {"uid": user_account_id},
                    )).fetchall()
                else:
                    rows = (await _s_h.execute(
                        _t_h("SELECT role, content FROM conversation_messages WHERE openid = :oid AND created_at >= CURRENT_DATE ORDER BY created_at DESC LIMIT 10"),
                        {"oid": openid or ""},
                    )).fetchall()
            for row in reversed(rows):
                messages.append({"role": row[0], "content": row[1]})
        except Exception as e:
            import logging
            logging.warning(f"[Session] 加载历史失败: {e}")
        
        # 检查是否有重启间隔 → 注入上下文告知 Hermes
        restart_msg = _detect_restart_gap()
        if restart_msg:
            logger.info("[Session] 检测到服务重启, 注入上下文: %%s", restart_msg)
            messages.append({
                "role": "system",
                "content": f"[系统通知] {restart_msg}。用户上一次的对话已加载，请自然承接上下文，不要主动提及技术细节。如果用户问起才解释。"
            })
        
        messages.append({"role": "user", "content": content})

        # 在熔断器保护下调用 Hermes API
        try:
            resp = await _hermes_cb.call(
                client.post(
                    hermes_url,
                    json={
                        "model": "hermes-agent",
                        "messages": messages,
                        "max_tokens": use_max_tokens,
                        "stream": False,
                    },
                    headers=headers,
                ),
                timeout=cb_timeout,
            )
        except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException, CircuitBreakerOpen) as cb_e:
            sem.release()
            is_cb_open = isinstance(cb_e, CircuitBreakerOpen)
            logger.warning(f"[CB] Hermes 调用失败({type(cb_e).__name__}): {cb_e}")
            # 不降级 DS（无上下文无意义），直接报错
            err_msg = "服务暂时不可用（AI 引擎忙），请稍后重试。"
            await _save_conversation_pair(session_key, user_account_id, openid, content, err_msg)
            return err_msg
        if resp.status_code == 200:
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            
            # 保全对话上下文（确保重启/降级后上下文不断）
            await _save_conversation_pair(session_key, user_account_id, openid, content, reply)
            
            # 无论是否有 session_key 都正常返回回复
            sem.release()
            return reply
        else:
            logger.warning(f"Hermes API 非 200 状态: {resp.status_code}")
            sem.release()
            return f"🤖 AI 引擎异常，请稍后重试。"
    except Exception as e:
        sem.release()
        logger.exception(f"Hermes API 调用失败: {e}")
        return f"🤖 服务暂时不可用"


async def _call_deepseek(content: str, user_nickname: str = "") -> str:
    """直接调 DeepSeek API"""
    import os
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return "🤖 DeepSeek 未配置"
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [
                        {"role": "system", "content": "你是享客虾 AI 助手,回答简洁实用。"},
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

@router.get("/paused")
async def bot_paused():
    """获取当前暂停的 Bot 列表"""
    import json, os
    pause_dir = os.path.expanduser("~/.hermes/bot_connectors/pause")
    paused = []
    if os.path.isdir(pause_dir):
        for f in os.listdir(pause_dir):
            if f.endswith(".json"):
                fpath = os.path.join(pause_dir, f)
                try:
                    data = json.loads(open(fpath).read())
                    until = data.get("paused_until", 0)
                    if until > __import__("time").time() * 1000:
                        bot_id = f.replace("_at_", "@").replace("_dot_", ".").replace(".json", "")
                        remaining = max(0, until - int(__import__("time").time() * 1000))
                        paused.append({"bot_id": bot_id, "remaining_ms": remaining})
                except:
                    pass
    return {"paused": [p["bot_id"] for p in paused], "details": paused}

@router.post("/push")
async def push_message(data: dict):
    """Push message to WeChat user via DB queue."""
    bot_id = data.get("bot_id", "")
    to_user = data.get("to_user", "")
    content = data.get("content", "")
    context_token = data.get("context_token", "")
    from_name = data.get("from_name", "")
    
    if not bot_id or not to_user or not content:
        return {"success": False, "error": "missing params: bot_id, to_user, content"}
    
    if from_name:
        content = f"来自《{from_name}》:{content}"
    
    # 写入 DB 推送队列(连接器会异步投递)
    try:
        from app.models import AsyncSessionLocal as _asf
        from sqlalchemy import text as _t
        async with _asf() as _s:
            await _s.execute(
                _t("INSERT INTO push_queue (bot_id, to_user, content, context_token) VALUES (:bid, :uid, :ct, :ctx)"),
                {"bid": bot_id, "uid": to_user, "ct": content, "ctx": context_token or ""},
            )
            await _s.commit()
        
        logger.info(f"[Push] 已入队 bot={bot_id[:20]} to={to_user[:20]}")
        return {"success": True, "message": "已入队,连接器将异步投递"}
    except Exception as e:
        logger.error(f"[Push] 入队失败: {e}")
        return {"success": False, "error": str(e)}
