"""
享客虾 — 共享 AI 对话服务
供微信、QQ、及其他通道复用
"""
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = f"{os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')}/v1/chat/completions"

# 订阅引导文案
NOT_SUBSCRIBED_MSG = (
    '🦞 嗨！我是**享客虾**，你的私人AI秘书。\n\n'
    '📱 开通后即可开始对话：\n'
    '🥉 基础版 · ¥9.9/月 · 500条\n'
    '🥈 标准版 · ¥19.9/月 · 2000条\n\n'
    '👉 请在微信中打开链接开通：{url}'
)

QUOTA_EXHAUSTED_MSG_TMPL = (
    '🦞 本月 {limit} 条额度已用完。\n\n'
    '👉 续费或升级套餐：{url}'
)

PRODUCT_URL = os.getenv('BASE_URL', 'http://xkx.pangoozn.com')
NOT_SUBSCRIBED_MSG = NOT_SUBSCRIBED_MSG.replace('{url}', PRODUCT_URL)
QUOTA_EXHAUSTED_MSG = QUOTA_EXHAUSTED_MSG_TMPL.replace('{url}', PRODUCT_URL)


async def ai_chat(nickname: str, messages: list, user_msg: str) -> str:
    """AI 回复 — 多通道共享"""
    if not DEEPSEEK_KEY:
        return '服务暂不可用。'

    system = (
        f'你是{nickname}的私人AI秘书"享客虾"。友好、贴心、高效。'
        f'回复简洁实用，200字以内。直接给答案，不废话。'
    )
    ctx = [{'role': 'system', 'content': system}]
    for m in (messages or [])[-10:]:
        ctx.append({'role': m.get('role', 'user'), 'content': m.get('content', '')[:500]})
    ctx.append({'role': 'user', 'content': user_msg[:2000]})

    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(DEEPSEEK_URL,
                headers={'Authorization': f'Bearer {DEEPSEEK_KEY}', 'Content-Type': 'application/json'},
                json={'model': 'deepseek-chat', 'messages': ctx, 'max_tokens': 500, 'temperature': 0.7})
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'].strip()
            return '（响应失败，请再试一次）'
    except Exception:
        return '（网络波动，请再发一次）'
