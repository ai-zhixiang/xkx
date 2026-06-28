with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

old = '''    if not phone or not phone.isdigit() or len(phone) != 11:
        return {"success": False, "error": "\u8bf7\u8f93\u5165\u6b63\u786e\u768411\u4f4d\u624b\u673a\u53f7"}

    import time as _t'''

new = '''    if not phone or not phone.isdigit() or len(phone) != 11:
        return {"success": False, "error": "\u8bf7\u8f93\u5165\u6b63\u786e\u768411\u4f4d\u624b\u673a\u53f7"}

    # before: check if phone already registered
    from app.models import AsyncSessionLocal as _asf_s
    from sqlalchemy import text as _st_s
    async with _asf_s() as _s_s:
        row_s = await _s_s.execute(
            _st_s("SELECT id FROM user_accounts WHERE phone = :p"),
            {"p": phone},
        )
        if row_s.fetchone():
            return {"success": True, "already_bound": True, "message": "\u8be5\u624b\u673a\u53f7\u5df2\u7ed1\u5b9a\uff0c\u6b22\u8fce\u56de\u6765 \U0001f389"}

    import time as _t'''

c = c.replace(old, new, 1)
print('Fix:', 'OK' if old not in c else 'FAIL')

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
    f.write(c)

import py_compile
py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
print('Syntax OK')
