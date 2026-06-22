#!/usr/bin/env python3
"""Fix the regex in bot_gateway.py line 1179 to avoid syntax error"""
path = '/home/ubuntu/weclaw-1/app/routes/bot_gateway.py'

with open(path) as f:
    c = f.read()

# Find the problematic regex line
old = """_stripped = _re2.sub(r'[,。!?、;:\\"'\\s]', '', content).lower()"""
new = """_stripped = _re2.sub(r'[\u3001\u3002\uff01\uff1f\uff0c\uff1b\uff1a\\s]', '', content).lower()"""

if old in c:
    c = c.replace(old, new)
    with open(path, 'w') as f:
        f.write(c)
    print('FIXED')
else:
    print('NOT FOUND')
    # Find similar line
    for i, line in enumerate(c.split('\n')):
        if 'stripped' in line and 're2' in line:
            print(f'Line {i+1}: {line}')

import ast
try:
    ast.parse(c)
    print('SYNTAX_OK')
except SyntaxError as e:
    print(f'SYNTAX_ERROR: {e}')
