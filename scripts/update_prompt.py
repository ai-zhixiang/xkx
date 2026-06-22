#!/usr/bin/env python3
"""Update the subscription prompt in bot_gateway.py to include a web link"""
path = '/home/ubuntu/weclaw-1/app/routes/bot_gateway.py'
with open(path) as f:
    c = f.read()

old = '''                    return (
                        "🦞 欢迎来到享客虾！\\n\\n"
                        "已绑定手机，但还未开通会员。\\n"
                        "回复「开通」使用 Mock 支付体验完整流程 🎁"
                    )'''

new = '''                    return (
                        "🦞 欢迎来到享客虾！\\n\\n"
                        "已绑定手机，但还未开通会员。\\n\\n"
                        "👉 点此开通：https://dev.pangoozn.com/static/subscribe.html"
                        "?user_id=" + user_id + "&openid=" + openid + "\\n\\n"
                        "或回复「开通」使用 Mock 支付"
                    )'''

if old in c:
    c = c.replace(old, new)
    with open(path, 'w') as f:
        f.write(c)
    print('UPDATED')
else:
    print('NOT_FOUND')

import ast
try:
    ast.parse(c)
    print('SYNTAX_OK')
except SyntaxError as e:
    print(f'SYNTAX_ERROR: {e}')
