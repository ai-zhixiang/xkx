"""
享客虾 — 微信菜单管理
"""
import os
import httpx
from urllib.parse import quote
from fastapi import APIRouter

router = APIRouter()

WX_APPID = os.getenv('WECHAT_APPID', '')
WX_APPSECRET = os.getenv('WECHAT_APPSECRET', '')
BASE_URL = os.getenv('BASE_URL', 'http://xkx.pangoozn.com')


async def get_access_token():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f'https://api.weixin.qq.com/cgi-bin/token'
            f'?grant_type=client_credential&appid={WX_APPID}&secret={WX_APPSECRET}'
        )
        return r.json().get('access_token', '')


@router.get('/api/menu/create')
async def create_menu():
    """创建公众号菜单"""
    token = await get_access_token()
    if not token:
        return {'error': '获取 token 失败'}

    oauth_url = (
        f'https://open.weixin.qq.com/connect/oauth2/authorize'
        f'?appid={WX_APPID}'
        f'&redirect_uri={quote(BASE_URL + "/api/auth/callback?redirect=/", safe="")}'
        f'&response_type=code&scope=snsapi_userinfo&state=xkx'
        f'#wechat_redirect'
    )

    menu = {
        'button': [
            {
                'name': '开通享客虾',
                'type': 'view',
                'url': oauth_url,
            },
            {
                'name': '📱 我的',
                'sub_button': [
                    {
                        'name': '查额度',
                        'type': 'click',
                        'key': 'check_quota',
                    },
                    {
                        'name': '帮助',
                        'type': 'click',
                        'key': 'help',
                    },
                ],
            },
        ]
    }

    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f'https://api.weixin.qq.com/cgi-bin/menu/create?access_token={token}',
            json=menu,
        )
        return r.json()
