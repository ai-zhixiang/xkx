import re

with open("/home/ubuntu/weclaw-1/app/routes/bot_gateway.py", "r") as f:
    content = f.read()

changes = [
    # 1. Read media_path from request
    ('    content = data.get("content", "")\n    \n    if not content:',
     '    content = data.get("content", "")\n    media_path = data.get("media_path", "")\n    \n    if not content and not media_path:'),
    
    # 2. Pass media_path to _route_to_ai
    ('    response = await _route_to_ai(bot_id, user_id, content, nickname, openid, user_account_id)',
     '    response = await _route_to_ai(bot_id, user_id, content, nickname, openid, user_account_id, media_path)'),
    
    # 3. Update _route_to_ai definition
    ('async def _route_to_ai(bot_id: str, user_id: str, content: str, user_nickname: str = "", openid: str = "", user_account_id: int = None) -> str:',
     'async def _route_to_ai(bot_id: str, user_id: str, content: str, user_nickname: str = "", openid: str = "", user_account_id: int = None, media_path: str = "") -> str:'),
    
    # 4. Update hermes call in _route_to_ai
    ('        return await _call_hermes(content, user_id, user_nickname, openid, user_account_id)',
     '        return await _call_hermes(content, user_id, user_nickname, openid, user_account_id, media_path)'),
    
    # 5. Update _call_hermes definition
    ('async def _call_hermes(content: str, user_id: str, user_nickname: str = "", openid: str = "", user_account_id: int = None) -> str:',
     'async def _call_hermes(content: str, user_id: str, user_nickname: str = "", openid: str = "", user_account_id: int = None, media_path: str = "") -> str:'),
    
    # 6. Add media hint to system prompt
    ('system_prompt = (\n        f"当前用户: {user_nickname or \'铭道\'} | OpenID: {(openid or \'\')[:16]}...\\n"\n        f"注意：你的 persona 和记忆已加载。用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。"\n    )',
     '''media_hint = ""
    if media_path:
        media_hint = f"\\n用户发来媒体文件: {media_path}。你可以读取并处理它。"
    
    system_prompt = (
        f"当前用户: {user_nickname or '铭道'} | OpenID: {(openid or '')[:16]}...{media_hint}\\n"
        f"注意：你的 persona 和记忆已加载。用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。"
    )'''),
]

for old, new in changes:
    if old in content:
        content = content.replace(old, new)
        print(f"  ✓ Replaced: {old[:50]}...")
    else:
        print(f"  ✗ NOT FOUND: {old[:50]}...")
        # Try to find similar text
        for line in content.split('\n'):
            if old[:30] in line:
                print(f"    Nearby: {line.strip()[:80]}")

with open("/home/ubuntu/weclaw-1/app/routes/bot_gateway.py", "w") as f:
    f.write(content)

print("\nDONE")
