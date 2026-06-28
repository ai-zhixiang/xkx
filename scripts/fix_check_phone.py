import re

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

# Find bind_send_code function and add phone check
old_func_start = '''async def bind_send_code(data: dict):
    """发送绑定验证码"""
    phone = (data.get("phone", "") or "").strip()

    if not phone or not re.match(r"^1\\d{10}$", phone):
        return {"success": False, "error": "请输入正确的11位手机号"}

    import time as _t
    _last = _bind_sms_last.get(phone, 0)
    if _t.time() - _last < 60:
        return {"success": False, "error": "请60秒后再试"}
    
    code = str(random.randint(100000, 999999))
    _bind_sms_codes[phone] = {"code": code, "expires_at": _t.time() + 300}
    _bind_sms_last[phone] = _t.time()
    
    if _send_sms_code(phone, code):'''

# Try to find the exact function
idx = c.find('async def bind_send_code')
if idx > 0:
    # Get the function up to the point it sends the SMS
    end_idx = c.find('if _send_sms_code(phone, code):', idx)
    if end_idx > 0:
        # Get the function body up to this point
        func_body = c[idx:end_idx]
        print('Found function body:')
        print(func_body)
        
        # Add phone check after regex validation, before rate limit
        old_check = '''    if not phone or not re.match(r"^1\\d{10}$", phone):
        return {"success": False, "error": "请输入正确的11位手机号"}'''
        
        new_check = '''    if not phone or not re.match(r"^1\\d{10}$", phone):
        return {"success": False, "error": "请输入正确的11位手机号"}

    # Check if phone already registered
    from app.models import AsyncSessionLocal as _asf_s
    from sqlalchemy import text as _st_s
    async with _asf_s() as _s_s:
        row_s = await _s_s.execute(
            _st_s("SELECT id FROM user_accounts WHERE phone = :p"),
            {"p": phone},
        )
        if row_s.fetchone():
            return {"success": True, "already_bound": True, "message": "该手机号已绑定，欢迎回来 🎉"}
    
    import time as _t
    _last = _bind_sms_last.get(phone, 0)'''
        
        c = c.replace(old_check, new_check, 1)
        if old_check not in c:
            print('Phone check added: OK')
        else:
            print('Phone check: FAIL')
        
        with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
            f.write(c)
        
        import py_compile
        py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
        print('Syntax OK')
    else:
        print('Could not find send code section')
else:
    print('Function not found')
    # Search for it
    for m in re.finditer(r'async def bind_send_code', c):
        print(f'  Found at {m.start()}')
