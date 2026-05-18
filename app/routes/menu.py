"""
享客虾 — 微信菜单管理
（与智享家共享服务号，主菜单由 ailuckycards 维护）
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
    """创建公众号菜单（完整版，含智享家全线产品）"""
    token = await get_access_token()
    if not token:
        return {'error': '获取 token 失败'}

    menu = {
        'button': [
            {
                'name': 'AI嗨卡',
                'sub_button': [
                    {'type': 'view', 'name': 'AI制卡', 'url': 'https://hai.pangoozn.com/static/ai-card.html'},
                    {'type': 'view', 'name': '每日分享', 'url': 'https://hai.pangoozn.com/static/daily.html'},
                    {'type': 'view', 'name': '个人中心', 'url': 'https://hai.pangoozn.com/static/workspace.html'},
                ]
            },
            {
                'name': '音乐广场',
                'sub_button': [
                    {'type': 'view', 'name': '听音乐', 'url': 'https://hai.pangoozn.com/static/music-square.html'},
                    {'type': 'view', 'name': 'AI写歌', 'url': 'https://hai.pangoozn.com/static/workspace.html?tab=compose'},
                ]
            },
            {
                'name': '品牌合作',
                'sub_button': [
                    {'type': 'view', 'name': '智享家', 'url': 'https://hai.pangoozn.com/'},
                    {'type': 'view', 'name': '🦞享客虾', 'url': 'https://hai.pangoozn.com/xkx/'},
                ]
            },
        ]
    }

    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f'https://api.weixin.qq.com/cgi-bin/menu/create?access_token={token}',
            json=menu,
        )
        return r.json()
