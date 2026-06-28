with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

old = 'INSERT INTO user_accounts (phone, nickname) VALUES (:p, :n) RETURNING id'
new = 'INSERT INTO user_accounts (phone, nickname) VALUES (:p, :n) ON CONFLICT (phone) DO UPDATE SET nickname = :n2 RETURNING id'

old_full = '''                r2 = await _s.execute(
                    _t2("INSERT INTO user_accounts (phone, nickname) VALUES (:p, :n) ON CONFLICT (phone) DO UPDATE SET nickname = :n2 RETURNING id"),
                    {"p": phone, "n": f"用户{phone[-4:]}", "n2": f"用户{phone[-4:]}"},
                )'''

# Check if already patched
if 'ON CONFLICT (phone)' in c:
    print('Already patched')
else:
    c = c.replace(old, new, 1)
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(c)
    
    # Need to add :n2 to params
    old_params = '{"p": phone, "n": f"用户{phone[-4:]}"},'
    new_params = '{"p": phone, "n": f"用户{phone[-4:]}", "n2": f"用户{phone[-4:]}"},'
    c2 = open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py').read()
    c2 = c2.replace(old_params, new_params, 1)
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(c2)
    
    print('Fix applied')

import py_compile
py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
print('Syntax OK')
