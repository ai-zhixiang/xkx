with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

old = '''    from app.models import AsyncSessionLocal as _asf_s
    from sqlalchemy import text as _st_s
    async with _asf_s() as _s_s:
        row_s = await _s_s.execute(
            _st_s("SELECT id FROM user_accounts WHERE phone = :p"),
            {"p": phone},
        )
        if row_s.fetchone():
            return {"success": True, "already_bound": True, "message": "\u8be5\u624b\u673a\u53f7\u5df2\u7ed1\u5b9a\uff0c\u6b22\u8fce\u56de\u6765 \U0001f389"}'''

new = '''    from app.models import AsyncSessionLocal as _asf_s
    from sqlalchemy import text as _st_s
    async with _asf_s() as _s_s:
        row_s = await _s_s.execute(
            _st_s("SELECT id FROM channel_bindings WHERE phone = :p AND openid = :oid"),
            {"p": phone, "oid": openid},
        )
        if row_s.fetchone():
            return {"success": True, "already_bound": True, "message": "\u8be5\u624b\u673a\u53f7\u5df2\u7ed1\u5b9a\uff0c\u6b22\u8fce\u56de\u6765 \U0001f389"}'''

if old in c:
    c = c.replace(old, new, 1)
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(c)
    import py_compile
    py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
    print('OK: channel_bindings check updated')
else:
    print('FAIL')
    # Debug: show exact bytes around the area
    idx = c.find('user_accounts WHERE phone')
    if idx > 0:
        print(repr(c[idx-30:idx+60]))
