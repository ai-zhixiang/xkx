with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    content = f.read()

import re

changes = 0

# Fix 1: allow web bind without channel_user_id
old1 = '''    if not parsed_cuid:
        return {"success": False, "error": "\u7ed1\u5b9a\u94fe\u63a5\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u4ece Bot \u83b7\u53d6\u94fe\u63a5"}
    
    channel_type = parsed_ct
    channel_user_id = parsed_cuid'''

new1 = '''    # web \u7ed1\u5b9a\uff08\u843d\u5730\u9875\uff09\u65e0 channel_user_id\uff0c\u53ea\u521b\u5efa user_account
    if not parsed_cuid and parsed_ct != "web":
        return {"success": False, "error": "\u7ed1\u5b9a\u94fe\u63a5\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u4ece Bot \u83b7\u53d6\u94fe\u63a5"}
    
    channel_type = parsed_ct
    channel_user_id = parsed_cuid'''

if old1 in content:
    content = content.replace(old1, new1, 1)
    changes += 1
    print('Fix 1: OK')
else:
    print('Fix 1: skipping (pattern changed)')

# Fix 2: skip channel_bindings for web binds
# Find the exact channel_bindings INSERT block
old2_pattern = r'await _s\.execute\(\s*_t2\("""INSERT INTO channel_bindings'
m = re.search(old2_pattern, content)
if m:
    # Find the full statement ending with ),
    start = m.start()
    # Find the closing ) followed by newline
    end = content.find('\n            )', start)
    if end > 0:
        old2 = content[start:end+14]  # include the closing )\n
        new2 = '''            if channel_type != "web":
''' + old2
        content = content.replace(old2, new2, 1)
        changes += 1
        print('Fix 2: OK (wrapped in if)')
    else:
        print('Fix 2: cannot find end of statement')
else:
    print('Fix 2: channel_bindings pattern not found')

if changes > 0:
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(content)
    print(f'Written {changes} changes')
    
    import py_compile
    py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
    print('Syntax OK')
else:
    print('No changes made')
