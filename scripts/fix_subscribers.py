with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

old = '''                    _t2("""INSERT INTO subscribers (openid, nickname, phone, plan_id, status, started_at, expires_at, created_at, updated_at)
                        VALUES (:oid, :nick, :p, 1, 'ACTIVE', CURRENT_DATE, :ea, NOW(), NOW())\"""")'''

new = '''                    _t2("""INSERT INTO subscribers (openid, nickname, plan_id, status, started_at, expires_at, created_at, updated_at)
                        VALUES (:oid, :nick, 1, 'ACTIVE', CURRENT_DATE, :ea, NOW(), NOW())\"""")'''

c = c.replace(old, new, 1)
print('Fix:', 'OK' if old not in c else 'FAIL')

# Also fix the params - remove :p
old_p = '{"oid": openid, "nick": f"用户{phone[-4:]}", "p": phone, "ea": trial_expires},'
new_p = '{"oid": openid, "nick": f"用户{phone[-4:]}", "ea": trial_expires},'
c = c.replace(old_p, new_p, 1)

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
    f.write(c)

import py_compile
py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
print('Syntax OK')
