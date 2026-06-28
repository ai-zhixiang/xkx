import re

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'r') as f:
    content = f.read()

old = '''    channel_user_id = ""
    if bind_code:
        info = _bind_codes.get(bind_code)
        if info:
            channel_user_id = info.get("channel_user_id", "")
        else:
            channel_user_id = bind_code

    if not channel_user_id:
        return {"success": False, "error": "绑定链接已失效，请重新从 Bot 获取链接"}'''

new = '''    # 从 bind_code 解析 channel info（内存 _bind_codes 可能因重启丢失，直接解析 URL）
    parsed_ct = channel_type
    parsed_cuid = ""
    if bind_code:
        parts = bind_code.split(":", 1)
        if len(parts) == 2:
            parsed_ct = parts[0]
            parsed_cuid = parts[1]
        else:
            parsed_cuid = bind_code

    if not parsed_cuid:
        return {"success": False, "error": "绑定链接已失效，请重新从 Bot 获取链接"}
    
    channel_type = parsed_ct
    channel_user_id = parsed_cuid'''

if old in content:
    content = content.replace(old, new, 1)
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(content)
    print('OK: patched')
else:
    print('FAIL: old string not found')
    m = re.search(r'channel_user_id.{0,300}请重新从 Bot 获取链接', content, re.DOTALL)
    if m:
        print('Found nearby:', repr(m.group()[:300]))
    else:
        print('Could not find the pattern at all')
