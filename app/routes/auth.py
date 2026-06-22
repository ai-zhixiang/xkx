"""
享客虾 — 微信 OAuth 授权（向后兼容）+ 统一用户触点
用户从公众号点链接 → OAuth → 拿 openid → 回产品页

统一用户体系：与 AI嗨卡/智享家 共享 openid 作为统一用户标识。
AI传薪计划、MV播放器、每日分享、公益项目均使用同一 openid。
"""


import os
import asyncio
import httpx
from urllib.parse import urlencode
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
import secrets as _secrets
import jwt
import logging as _logging
_logger = _logging.getLogger('weclawd.auth')
_oauth_sessions: dict = {}

router = APIRouter()

WX_APPID = os.getenv('WECHAT_APPID', '')
WX_APPSECRET = os.getenv('WECHAT_APPSECRET', '')
JWT_SECRET = os.getenv('JWT_SECRET', '')
JWT_EXPIRE_DAYS = 30
BASE_URL = os.getenv('BASE_URL', 'http://xkx.pangoozn.com')


def _make_jwt(openid: str, nickname: str = '', avatar: str = '') -> str:
    import datetime
    payload = {
        'openid': openid,
        'nickname': nickname,
        'avatar': avatar,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=JWT_EXPIRE_DAYS),
        'iat': datetime.datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

async def _save_user_profile(openid: str, nickname: str, avatar: str):
    """保存微信用户信息到订阅者记录（仅已订阅用户）"""
    try:
        import asyncpg
        dsn = os.getenv('DATABASE_URL', 'postgresql://lucky:lucky_pass@localhost/weclawd')
        dsn = dsn.replace('postgresql+asyncpg://', 'postgresql://')
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            await conn.execute(
                "UPDATE subscribers SET nickname = $1, avatar_url = $2, updated_at = NOW() WHERE openid = $3",
                nickname, avatar, openid
            )
        finally:
            await conn.close()
    except Exception as e:
        import logging
        logging.getLogger('weclawd').warning(f'[save_profile] 保存头像昵称失败: {e}')


@router.get('/api/auth/redirect')
async def auth_redirect(redirect: str = '/'):
    """跳转微信 OAuth 授权页"""
    callback = f'{BASE_URL}/api/auth/callback?redirect={redirect}'
    params = {
        'appid': WX_APPID,
        'redirect_uri': callback,
        'response_type': 'code',
        'scope': 'snsapi_userinfo',
        'state': 'xkx',
    }
    url = f'https://open.weixin.qq.com/connect/oauth2/authorize?{urlencode(params)}#wechat_redirect'
    return RedirectResponse(url)


@router.get('/api/auth/callback')
async def auth_callback(code: str = '', state: str = '', redirect: str = '/'):
    """OAuth 回调：code → openid → 重定向"""
    if not code:
        return RedirectResponse(f'{BASE_URL}/?error=no_code')

    # 换 access_token
    token_url = (
        f'https://api.weixin.qq.com/sns/oauth2/access_token'
        f'?appid={WX_APPID}&secret={WX_APPSECRET}&code={code}&grant_type=authorization_code'
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(token_url)
            data = r.json()
            openid = data.get('openid', '')
            access_token = data.get('access_token', '')
            if not openid:
                return RedirectResponse(f'{BASE_URL}/?error=oauth_fail')
            
            # 拉取用户昵称和头像
            nickname = ''
            avatar = ''
            if access_token:
                try:
                    ui = await client.get(
                        'https://api.weixin.qq.com/sns/userinfo',
                        params={'access_token': access_token, 'openid': openid, 'lang': 'zh_CN'}
                    )
                    ud = ui.json()
                    if ud.get('nickname'):
                        nickname = ud['nickname']
                        avatar = ud.get('headimgurl', '')
                except Exception:
                    pass

            # 通过 JS 写入 localStorage 然后跳转（微信内无法设 cookie）
            # 兼容相对路径（/xkx/）和绝对 URL
            final_url = redirect if redirect.startswith('http') else f'{BASE_URL}{redirect}'
            import json
            safe_openid = json.dumps(openid)  # JSON 编码防 XSS
            
            # 后台异步保存头像昵称到 subscribers 表
            asyncio.create_task(_save_user_profile(openid, nickname, avatar))
            
            html = f'''<!DOCTYPE html><html><head><meta charset="UTF-8"></head><body>
<script>
localStorage.setItem('wx_openid', {safe_openid});
localStorage.setItem('wx_nickname', {json.dumps(nickname)});
localStorage.setItem('wx_avatar', {json.dumps(avatar)});
location.href = {json.dumps(final_url)};
</script></body></html>'''
            from fastapi.responses import HTMLResponse
            return HTMLResponse(html)
    except Exception:
        return RedirectResponse(f'{BASE_URL}/?error=network')

# ===== 统一用户体系：与 AI嗨卡 共享 openid 用户身份 =====

# ===== 通道绑定 =====

@router.post('/api/auth/bind')
async def bind_channel(data: dict):
    """通道绑定：OAuth 回调后写入 channel_bindings 表
    绑定 channel_type + channel_user_id → openid + nickname
    """
    import asyncpg
    dsn = os.getenv('DATABASE_URL', 'postgresql://lucky:lucky_pass@localhost/weclawd')
    dsn = dsn.replace('postgresql+asyncpg://', 'postgresql://')
    
    channel_type = data.get('channel_type', '')
    channel_user_id = data.get('channel_user_id', '')
    openid = data.get('openid', '')
    nickname = data.get('nickname', '')
    
    if not all([channel_type, channel_user_id, openid]):
        return {"success": False, "error": "缺少必填参数"}
    
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            await conn.execute("""
                INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname, is_active, bound_at)
                VALUES ($1, $2, $3, $4, true, NOW())
                ON CONFLICT (channel_type, channel_user_id) 
                DO UPDATE SET openid = $3, nickname = COALESCE(NULLIF($4, ''), channel_bindings.nickname), is_active = true, bound_at = NOW()
            """, channel_type, channel_user_id, openid, nickname)
            return {"success": True, "bound": True, "openid": openid, "nickname": nickname}
        finally:
            await conn.close()
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get('/api/auth/bind-check')
async def check_binding(channel_type: str = '', channel_user_id: str = ''):
    """查询通道是否已绑定用户"""
    import asyncpg
    dsn = os.getenv('DATABASE_URL', 'postgresql://lucky:lucky_pass@localhost/weclawd')
    dsn = dsn.replace('postgresql+asyncpg://', 'postgresql://')
    
    if not all([channel_type, channel_user_id]):
        return {"bound": False, "error": "缺少参数"}
    
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            row = await conn.fetchrow(
                "SELECT openid, nickname, bound_at FROM channel_bindings WHERE channel_type = $1 AND channel_user_id = $2 AND is_active = true",
                channel_type, channel_user_id
            )
            if row:
                return {"bound": True, "openid": row['openid'], "nickname": row['nickname'], "bound_at": str(row['bound_at'])}
            return {"bound": False}
        finally:
            await conn.close()
    except Exception as e:
        return {"bound": False, "error": str(e)}


@router.post('/api/auth/touch')
async def touch_user(request: Request):
    """统一用户触点：接收主站传来的 openid + device_uuid，自动在享客虾侧建立用户记录。
    即使未付费/未订阅也入库，确保跨产品用户身份统一。
    与主站 /api/auth/touch 对应，双向同步。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    openid = body.get('openid', '')
    device_uuid = body.get('device_uuid', '')

    if not openid and not device_uuid:
        return {"ok": False, "error": "need openid or device_uuid"}

    try:
        import asyncpg
        dsn = os.getenv('DATABASE_URL', 'postgresql://lucky:lucky_pass@localhost/weclawd')
        dsn = dsn.replace('postgresql+asyncpg://', 'postgresql://')
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            if openid:
                existing = await conn.fetchrow(
                    "SELECT id FROM subscribers WHERE openid = $1", openid
                )
                if existing:
                    if device_uuid:
                        await conn.execute(
                            "UPDATE subscribers SET device_uuid = $1, updated_at = NOW() WHERE openid = $2 AND (device_uuid IS NULL OR device_uuid = '')",
                            device_uuid, openid
                        )
                else:
                    await conn.execute(
                        """INSERT INTO subscribers (openid, device_uuid, status, started_at, expires_at, messages_used, messages_limit)
                           VALUES ($1, $2, 'visitor', CURRENT_DATE, CURRENT_DATE, 0, 0)
                           ON CONFLICT (openid) DO UPDATE SET device_uuid = COALESCE(subscribers.device_uuid, $2)""",
                        openid, device_uuid or ''
                    )
            elif device_uuid:
                # device_uuid only — ensure at least a device profile exists
                await conn.execute(
                    """INSERT INTO subscribers (openid, device_uuid, status, plan_id, started_at, expires_at, messages_used, messages_limit)
                       VALUES ($1, $2, 'visitor', 0, CURRENT_DATE, CURRENT_DATE, 0, 0)
                       ON CONFLICT DO NOTHING""",
                    'device:' + device_uuid[:28], device_uuid
                )
        finally:
            await conn.close()
        return {"ok": True, "openid": openid, "device_uuid": device_uuid}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def _code2session(code: str) -> dict:
    """微信 code → openid + access_token"""
    url = (
        f"https://api.weixin.qq.com/sns/oauth2/access_token"
        f"?appid={WX_APPID}&secret={WX_APPSECRET}&code={code}&grant_type=authorization_code"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            return r.json()
    except Exception as e:
        _logger.warning(f"[_code2session] 失败: {e}")
        return {}

async def _get_userinfo(access_token: str, openid: str) -> dict:
    """获取微信用户信息（昵称+头像）"""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.weixin.qq.com/sns/userinfo",
                params={"access_token": access_token, "openid": openid, "lang": "zh_CN"}
            )
            return r.json()
    except Exception as e:
        _logger.warning(f"[_get_userinfo] 失败: {e}")
        return {}

async def _sync_profile_to_production(openid: str, nickname: str, avatar_url: str):
    """回调深圳生产库，写入用户统计"""
    try:
        from fastapi.responses import JSONResponse
        async with httpx.AsyncClient(verify=False, timeout=5) as c:
            await c.post(
                "https://hai.pangoozn.com/api/sync/wechat-profile",
                json={"openid": openid, "nickname": nickname, "avatar_url": avatar_url},
            )
    except Exception as e:
        _logger.warning(f"[sync_profile] 生产库同步失败: {e}")


@router.get('/api/auth/wechat-redirect')
async def wechat_redirect(
    target: str = '/static/hai.html',
    device_uuid: str = '',
    source: str = '',
    bind: str = '',
    direct: int = 0,
):
    """生成微信 OAuth 授权 URL（统一认证入口）"""
    session_id = _secrets.token_hex(8)
    _oauth_sessions[session_id] = {"target": target, "du": device_uuid, "source": source, "bind": bind}
    redirect_uri = "https://hai.pangoozn.com/api/auth/wechat-callback"
    from urllib.parse import quote
    auth_url = (
        "https://open.weixin.qq.com/connect/oauth2/authorize"
        f"?appid={WX_APPID}"
        f"&redirect_uri={quote(redirect_uri)}"
        "&response_type=code"
        "&scope=snsapi_userinfo"
        f"&state={session_id}"
        "#wechat_redirect"
    )
    if direct:
        return RedirectResponse(url=auth_url)
    return {"redirect_url": auth_url}


@router.get('/api/auth/wechat-callback')
async def wechat_callback(
    code: str = '',
    state: str = '',
):
    """微信 OAuth 回调（统一认证）"""
    from fastapi.responses import RedirectResponse, HTMLResponse
    from urllib.parse import quote as _quote
    
    if not code:
        return RedirectResponse(url="/static/bind_success.html?error=no_code")
    
    # 1. 解析 session
    session_data = _oauth_sessions.get(state, {})
    target = session_data.get("target", "/static/bind_success.html")
    bind_param = session_data.get("bind", "")
    source = session_data.get("source", "bot_bind")
    
    # 2. code → openid
    wx_data = await _code2session(code)
    openid = wx_data.get("openid", "")
    access_token = wx_data.get("access_token", "")
    if not openid:
        return RedirectResponse(url="/static/bind_success.html?error=oauth_fail")
    
    # 3. 获取用户信息
    nickname = ""
    avatar_url = ""
    if access_token:
        info = await _get_userinfo(access_token, openid)
        nickname = info.get("nickname", "") or ""
        avatar_url = info.get("headimgurl", "") or ""
    
    # 4. 通道绑定（如有 bind 参数）
    if bind_param and ":" in bind_param:
        try:
            parts = bind_param.split(":", 1)
            b_type, b_value = parts[0], parts[1]
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(
                    "http://127.0.0.1:8001/api/bot/bind",
                    json={
                        "channel_type": b_type,
                        "channel_user_id": b_value,
                        "openid": openid,
                        "nickname": nickname,
                    }
                )
            _logger.info(f"[绑定] {b_type}:{b_value[:20]}... → {openid[:15]}... ({nickname})")
            target = "/static/bind_success.html"
        except Exception as e:
            _logger.warning(f"[绑定] 失败: {e}")
    
    # 5. 同步用户信息到生产库（异步，不阻塞）
    asyncio.create_task(_sync_profile_to_production(openid, nickname, avatar_url))
    
    # 6. 生成 JWT token 并重定向到目标页面
    jwt_token = _make_jwt(openid, nickname, avatar_url)
    from urllib.parse import urlencode as _urlencode
    params = _urlencode({"openid": openid, "nickname": nickname, "avatar": avatar_url, "source": source, "token": jwt_token})
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{sep}{params}")

@router.get('/api/auth/bot-oauth')
async def bot_oauth_redirect(target: str = '/static/bot.html'):
    """Bot 扫码页 OAuth：代理 hai.pangoozn.com 的 OAuth，返回 302 跳转"""
    import httpx
    url = f'https://hai.pangoozn.com/api/auth/wechat-redirect?target={__import__("urllib").parse.quote(target)}&source=bot'
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            data = r.json()
            if data.get('redirect_url'):
                from fastapi.responses import RedirectResponse
                return RedirectResponse(data['redirect_url'])
    except Exception as e:
        _logger.error(f'bot-oauth proxy failed: {e}')
    return {'error': 'OAuth 失败'}
