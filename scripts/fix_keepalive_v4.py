import re, sys

with open('/home/ubuntu/weclaw-keepalive/keepalive_service.py', 'r') as f:
    content = f.read()

# Replace the broken helper function with a clean version
old_fn = '''async def _build_plan_prices_text():
    """从 DB 读取套餐，生成价格文本（单行+多行）"""
    global _plan_prices_cache, _plan_prices_ts
    now = time.time()
    if _plan_prices_cache and now - _plan_prices_ts < 3600:
        return _plan_prices_cache
    
    _plan_prices_cache = "• 月卡 ¥99/月（10000虾点）\\n• 年卡 ¥999/年（120000虾点）"
    _plan_prices_one_line = "月卡¥99/月 · 年卡¥999/年"
    try:
        import asyncpg
        ndb = await asyncpg.connect(DB_DSN)
        try:
            rows = await ndb.fetch("SELECT id, name, price, months FROM plans WHERE is_active = true ORDER BY sort_order LIMIT 4")
            lines = []
            cheap_monthly = None
            cheap_yearly = None
            for r in rows:
                price_yuan = r["price"] / 100
                points = r["months"] * 10000
                lines.append(f"• {r['name']} ¥{price_yuan:.0f}/{r['months']}个月（{int(points)}虾点）")
                if r["months"] == 1 and (cheap_monthly is None or r["price"] < cheap_monthly):
                    cheap_monthly = r["price"]
                    _monthly_yuan = price_yuan
                if r["months"] == 12 and (cheap_yearly is None or r["price"] < cheap_yearly):
                    cheap_yearly = r["price"]
                    _yearly_yuan = price_yuan
            if cheap_monthly and cheap_yearly:
                _plan_prices_cache = "\\n".join(lines[:2])
                _plan_prices_one_line = f"月卡¥{_monthly_yuan:.0f}/月 · 年卡¥{_yearly_yuan:.0f}/年"
        finally:
            await ndb.close()
    except Exception:
        pass
    _plan_prices_ts = now
    global _plan_prices_one_line
    _plan_prices_one_line = _plan_prices_one_line
    return _plan_prices_cache

# Module-level cache
_plan_prices_cache = None
_plan_prices_ts = 0
_plan_prices_one_line = "月卡¥99/月 · 年卡¥999/年"'''

new_fn = '''# Module-level price text (fallback, overridden by DB query)
_plan_prices_multi = "• 月卡 ¥99/月（10000虾点）\\n• 年卡 ¥999/年（120000虾点）"
_plan_prices_one = "月卡¥99/月 · 年卡¥999/年"

async def _build_plan_prices_text():
    """从 DB 读取套餐，生成价格文本"""
    global _plan_prices_multi, _plan_prices_one, _plan_prices_ts
    now = time.time()
    if _plan_prices_multi and now - _plan_prices_ts < 3600:
        return _plan_prices_multi
    try:
        import asyncpg
        ndb = await asyncpg.connect(DB_DSN)
        try:
            rows = await ndb.fetch("SELECT id, name, price, months, sort_order FROM plans WHERE is_active = true ORDER BY sort_order LIMIT 8")
            multi = []
            cheap_monthly = _cheap_yearly = None
            monthly_y = yearly_y = 0
            for r in rows:
                y = r["price"] / 100
                pts = r["months"] * 10000
                multi.append(f"• {r['name']} ¥{y:.0f}/{r['months']}个月（{int(pts)}虾点）")
                if r["months"] == 1 and (cheap_monthly is None or r["price"] < cheap_monthly):
                    cheap_monthly = r["price"]; monthly_y = y
                if r["months"] == 12 and (cheap_yearly is None or r["price"] < cheap_yearly):
                    cheap_yearly = r["price"]; yearly_y = y
            if cheap_monthly and cheap_yearly:
                _plan_prices_multi = "\\n".join(multi[:2])
                _plan_prices_one = f"月卡¥{monthly_y:.0f}/月 · 年卡¥{yearly_y:.0f}/年"
        finally:
            await ndb.close()
    except Exception:
        pass
    _plan_prices_ts = now
    return _plan_prices_multi'''

content = content.replace(old_fn, new_fn)

# Fix welcome message reference _plan_prices_one_line -> _plan_prices_one
content = content.replace('_plan_prices_one_line', '_plan_prices_one')

with open('/home/ubuntu/weclaw-keepalive/keepalive_service.py', 'w') as f:
    f.write(content)

try:
    compile(content, 'keepalive_service.py', 'exec')
    print('SYNTAX OK')
except SyntaxError as e:
    print(f'SYNTAX ERROR: {e}')
    sys.exit(1)
