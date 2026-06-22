#!/usr/bin/env python3
"""Fix the mock-subscribe SQL - add started_at column"""
path = '/home/ubuntu/weclaw-1/app/routes/bot_gateway.py'
with open(path) as f:
    c = f.read()

old_sql = '''_t("INSERT INTO subscribers (openid, status, expires_at, created_at) "
                   "VALUES (:oid, 'ACTIVE', :exp, NOW()) "
                   "ON CONFLICT (openid) DO UPDATE SET status='ACTIVE', expires_at=:exp2"),'''

new_sql = '''_t("INSERT INTO subscribers (openid, status, started_at, expires_at, created_at) "
                   "VALUES (:oid, 'ACTIVE', NOW()::date, :exp, NOW()) "
                   "ON CONFLICT (openid) DO UPDATE SET status='ACTIVE', expires_at=:exp2"),'''

if old_sql in c:
    c = c.replace(old_sql, new_sql)
    with open(path, 'w') as f:
        f.write(c)
    print('SQL_FIXED')
else:
    print('NOT_FOUND')

import ast
try:
    ast.parse(c)
    print('SYNTAX_OK')
except SyntaxError as e:
    print(f'SYNTAX_ERROR: {e}')
