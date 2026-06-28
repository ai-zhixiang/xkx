with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

old = '''        return {"success": False, "error": "\u8bf7\u8f93\u5165\u6b63\u786e\u768411\u4f4d\u624b\u673a\u53f7"}

    # Check if phone+openid already bound
    if openid:
        from app.models import AsyncSessionLocal as _asf_s
        from sqlalchemy import text as _st_s
        async with _asf_s() as _s_s:
            row_s = await _s_s.execute(
                _st_s("SELECT id FROM channel_bindings WHERE phone = :p AND openid = :oid"),
                {"p": phone, "oid": openid},
            )
            if row_s.fetchone():
                return {"success": True, "already_bound": True, "message": "\u8be5\u624b\u673a\u53f7\u5df2\u7ed1\u5b9a\uff0c\u6b22\u8fce\u56de\u6765 \U0001f389"}

    import time as _t
    _last = _bind_sms_last.get(phone, 0)'''

new = '''        return {"success": False, "error": "\u8bf7\u8f93\u5165\u6b63\u786e\u768411\u4f4d\u624b\u673a\u53f7"}

    # Check if the exact phone+openid combo is already bound (same user)
    if openid:
        from app.models import AsyncSessionLocal as _asf_s
        from sqlalchemy import text as _st_s
        async with _asf_s() as _s_s:
            row_s = await _s_s.execute(
                _st_s("SELECT id FROM channel_bindings WHERE phone = :p AND openid = :oid"),
                {"p": phone, "oid": openid},
            )
            if row_s.fetchone():
                return {"success": True, "already_bound": True, "message": "\u8be5\u624b\u673a\u53f7\u5df2\u7ed1\u5b9a\uff0c\u6b22\u8fce\u56de\u6765 \U0001f389"}

    import time as _t
    _last = _bind_sms_last.get(phone, 0)'''

if old in c:
    c = c.replace(old, new, 1)
    print('OK: updated comment')
else:
    # Check if already correct
    if 'channel_bindings WHERE phone = :p AND openid = :oid' in c:
        print('Already correct')
    else:
        print('FAIL: pattern not found')
        # Show what's around
        idx = c.find('already_bound')
        if idx > 0:
            print('Found already_bound at', idx)
            print(repr(c[idx-50:idx+150]))

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
    f.write(c)

import py_compile
py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
print('Syntax OK')
