with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

changes = 0

# Fix 2: verify-code - add openid param
old2 = 'async def bind_verify_code(data: dict):\n    """\u9a8c\u8bc1\u77ed\u4fe1\u9a8c\u8bc1\u7801\u5e76\u5b8c\u6210\u7ed1\u5b9a"""\n    phone = (data.get("phone", "") or "").strip()\n    code = (data.get("code", "") or "").strip()\n    bind_code = (data.get("bind_code", "") or "").strip()\n    channel_type = data.get("channel_type", "ilink")'

new2 = 'async def bind_verify_code(data: dict):\n    """\u9a8c\u8bc1\u77ed\u4fe1\u9a8c\u8bc1\u7801\u5e76\u5b8c\u6210\u7ed1\u5b9a"""\n    phone = (data.get("phone", "") or "").strip()\n    code = (data.get("code", "") or "").strip()\n    bind_code = (data.get("bind_code", "") or "").strip()\n    channel_type = data.get("channel_type", "ilink")\n    openid = (data.get("openid", "") or "").strip()'

if old2 in c:
    c = c.replace(old2, new2, 1)
    if old2 not in c:
        changes += 1
        print('Fix 2: OK')
else:
    print('Fix 2: not found')

# Fix 3: conditional openid
old3 = '    openid = f"phone:{phone}"'
new3 = '    if not openid:\n        openid = f"phone:{phone}"'
if old3 in c:
    c = c.replace(old3, new3, 1)
    if old3 not in c:
        changes += 1
        print('Fix 3: OK')
else:
    print('Fix 3: not found')

if changes > 0:
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(c)
    import py_compile
    py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
    print(f'Syntax OK. {changes} changes.')
else:
    print('No changes')
