with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

import re

changes = 0

# Fix 1: send-code - check phone+openid combo
old1 = '''async def bind_send_code(data: dict):
    """\u53d1\u9001\u7ed1\u5b9a\u9a8c\u8bc1\u7801"""
    phone = (data.get("phone", "") or "").strip()
    if not phone or not phone.isdigit() or len(phone) != 11:
        return {"success": False, "error": "\u8bf7\u8f93\u5165\u6b63\u786e\u768411\u4f4d\u624b\u673a\u53f7"}

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

    import time as _t'''

# Check if already patched
if 'openid = (data.get("openid"' in c:
    print('Already patched with openid param')
else:
    # Find the send_code function and add openid param
    old1a = '''async def bind_send_code(data: dict):
    """\u53d1\u9001\u7ed1\u5b9a\u9a8c\u8bc1\u7801"""
    phone = (data.get("phone", "") or "").strip()'''
    
    new1a = '''async def bind_send_code(data: dict):
    """\u53d1\u9001\u7ed1\u5b9a\u9a8c\u8bc1\u7801"""
    phone = (data.get("phone", "") or "").strip()
    openid = (data.get("openid", "") or "").strip()'''
    
    c = c.replace(old1a, new1a, 1)
    if old1a not in c:
        changes += 1
        print('Fix 1a: openid param added to send_code')
    else:
        print('Fix 1a: FAIL')
    
    # Add check after phone validation
    old_check = '''        return {"success": False, "error": "\u8bf7\u8f93\u5165\u6b63\u786e\u768411\u4f4d\u624b\u673a\u53f7"}

    import time as _t'''
    
    new_check = '''        return {"success": False, "error": "\u8bf7\u8f93\u5165\u6b63\u786e\u768411\u4f4d\u624b\u673a\u53f7"}

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

    import time as _t'''
    
    c = c.replace(old_check, new_check, 1)
    if old_check not in c:
        changes += 1
        print('Fix 1b: already_bound check added')
    else:
        print('Fix 1b: FAIL')

# Fix 2: verify-code - accept openid param
old2 = '''async def bind_verify_code(data: dict):
    """\u9a8c\u8bc1\u77ed\u4fe1\u9a8c\u8bc1\u7801\u5e76\u5b8c\u6210\u7ed1\u5b9a"""
    phone = (data.get("phone", "") or "").strip()
    code = (data.get("code", "") or "").strip()
    bind_code = (data.get("bind_code", "") or "").strip()
    channel_type = data.get("channel_type", "ilink")'''

new2 = '''async def bind_verify_code(data: dict):
    """\u9a8c\u8bc1\u77ed\u4fe1\u9a8c\u8bc1\u7801\u5e76\u5b8c\u6210\u7ed1\u5b9a"""
    phone = (data.get("phone", "") or "").strip()
    code = (data.get("code", "") or "").strip()
    bind_code = (data.get("bind_code", "") or "").strip()
    channel_type = data.get("channel_type", "ilink")
    openid = (data.get("openid", "") or "").strip()'''

if new2 not in c and old2 in c:
    c = c.replace(old2, new2, 1)
    if old2 not in c:
        changes += 1
        print('Fix 2: openid param added to verify_code')
else:
    print('Fix 2: skip (already done or not found)')

# Fix 3: use openid from param instead of phone:xxx
old3 = '    if not openid:\n        openid = f"phone:{phone}"'
if old3 in c:
    print('Fix 3: already patched')
else:
    old3a = '    openid = f"phone:{phone}"'
    new3a = '    if not openid:\n        openid = f"phone:{phone}"'
    if old3a in c:
        c = c.replace(old3a, new3a, 1)
        if old3a not in c:
            changes += 1
            print('Fix 3: openid conditional set')

if changes > 0:
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(c)
    import py_compile
    py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
    print(f'Syntax OK. {changes} changes.')
else:
    print('No changes needed')
