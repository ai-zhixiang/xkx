with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    content = f.read()

# === Fix: verify-code support web-only binding (no channel) ===
old = '''    # 从 bind_code 解析 channel info（内存 _bind_codes 可能因重启丢失，直接解析 URL）
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

new = '''    # 从 bind_code 解析 channel info
    parsed_ct = channel_type
    parsed_cuid = ""
    if bind_code:
        parts = bind_code.split(":", 1)
        if len(parts) == 2:
            parsed_ct = parts[0]
            parsed_cuid = parts[1]
        else:
            parsed_cuid = bind_code

    # web 绑定（落地页）没有 channel_user_id，没问题
    if not parsed_cuid and parsed_ct != "web":
        return {"success": False, "error": "绑定链接已失效，请重新从 Bot 获取链接"}
    
    channel_type = parsed_ct
    channel_user_id = parsed_cuid'''

content = content.replace(old, new, 1)
print('Patch applied' if old in content else 'Old string gone, applied again' if old in content else 'Applying...')

if old not in content and 'parsed_cuid and parsed_ct != "web"' in content:
    print('Already patched')
else:
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(content)
    
    # Also patch the DB insert: when channel_type is "web", skip channel_bindings insert
    old2 = '''            await _s.execute(
                _t2("""INSERT INTO channel_bindings (channel_type, channel_user_id, openid, nickname, phone, user_account_id, welcomed, bound_at)
                    VALUES (:ct, :cuid, :oid, :nick, :p, :uid, true, NOW())
                    ON CONFLICT (channel_type, channel_user_id)
                    DO UPDATE SET openid = :oid2, nickname = :nick2, phone = :p2, user_account_id = :uid2, welcomed = true, bound_at = NOW()"""),
                {"ct": channel_type, "cuid": channel_user_id, "oid": openid, "nick": f"用户{phone[-4:]}}}
'''

if 'channel_type, channel_user_id' in content:
    print('channel_bindings INSERT found')

import py_compile
py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
print('Syntax OK')
