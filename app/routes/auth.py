"""
享客虾 — 微信 OAuth 授权
用户从公众号点链接 → OAuth → 拿 openid → 回产品页
"""
import os
import httpx
from urllib.parse import urlencode
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

WX_APPID = os.getenv('WECHAT_APPID', '')
WX_APPSECRET = os.getenv('WECHAT_APPSECRET', '')
BASE_URL = os.getenv('BASE_URL', 'http://xkx.pangoozn.com')


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
            if not openid:
                return RedirectResponse(f'{BASE_URL}/?error=oauth_fail')

            # 通过 JS 写入 localStorage 然后跳转（微信内无法设 cookie）
            html = f'''<!DOCTYPE html><html><head><meta charset="UTF-8"></head><body>
<script>
localStorage.setItem('wx_openid', '{openid}');
localStorage.setItem('wx_token', '{data.get("access_token", "")}');
location.href = '{BASE_URL}{redirect}';
</script></body></html>'''
            from fastapi.responses import HTMLResponse
            return HTMLResponse(html)
    except Exception:
        return RedirectResponse(f'{BASE_URL}/?error=network')
