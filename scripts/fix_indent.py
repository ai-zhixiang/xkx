with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    content = f.read()

old = '''                        if channel_type != "web":
await _s.execute(
                _t2("""INSERT INTO channel_bindings'''

new = '''            if channel_type != "web":
                await _s.execute(
                    _t2("""INSERT INTO channel_bindings'''

content = content.replace(old, new, 1)
print('Fix:', 'OK' if old not in content else 'FAIL')

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
    f.write(content)

import py_compile
py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
print('Syntax OK')
