with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

# Fix 1: add openid param to send_code
old1 = '    phone = (data.get("phone", "") or "").strip()\n    if not phone or not phone.isdigit() or len(phone) != 11:'
new1 = '    phone = (data.get("phone", "") or "").strip()\n    openid = (data.get("openid", "") or "").strip()\n    if not phone or not phone.isdigit() or len(phone) != 11:'

if old1 in c and new1 not in c:
    c = c.replace(old1, new1, 1)
    print('Fix 1: openid param added')
else:
    print('Fix 1: skip')

# Fix 2: change to channel_bindings with phone+openid
old2 = '_st_s("SELECT id FROM user_accounts WHERE phone = :p")\n            {"p": phone},\n        )\n        if row_s.fetchone():'
new2 = '_st_s("SELECT id FROM channel_bindings WHERE phone = :p AND openid = :oid")\n            {"p": phone, "oid": openid},\n        )\n        if row_s.fetchone():'

if old2 in c and new2 not in c:
    c = c.replace(old2, new2, 1)
    print('Fix 2: channel_bindings check added')
else:
    print('Fix 2: skip')

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
    f.write(c)

import py_compile
py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
print('Syntax OK')
