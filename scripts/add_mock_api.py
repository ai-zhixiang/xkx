#!/usr/bin/env python3
"""Add mock-subscribe endpoint before sync-subscriber in bot_gateway.py"""
path = '/home/ubuntu/weclaw-1/app/routes/bot_gateway.py'
with open(path) as f:
    c = f.read()

marker = '@router.post("/sync-subscriber")'
new_endpoint = '''@router.post("/mock-subscribe")
async def mock_subscribe(data: dict):
    """Mock 支付开通接口：写入 subscribers + 生成二维码"""
    user_id = data.get("user_id", "")
    openid = data.get("openid", user_id)
    bot_id = data.get("bot_id", "")

    if not user_id:
        return {"success": False, "error": "缺少 user_id"}

    try:
        from app.models import AsyncSessionLocal as _asf
        from sqlalchemy import text as _t
        import datetime

        async with _asf() as _s:
            await _s.execute(
                _t("INSERT INTO subscribers (openid, status, plan, expires_at, created_at) "
                   "VALUES (:oid, 'ACTIVE', 'mock', :exp, NOW()) "
                   "ON CONFLICT (openid) DO UPDATE SET status='ACTIVE', expires_at=:exp2"),
                {
                    "oid": openid,
                    "exp": datetime.date.today() + datetime.timedelta(days=30),
                    "exp2": datetime.date.today() + datetime.timedelta(days=30),
                },
            )
            await _s.commit()

        qr = await _fetch_qrcode()
        return {
            "success": True,
            "message": "开通成功，30天会员已激活",
            "qrcode_url": qr["qrcode_url"] if qr else "",
            "qrcode_img": qr["qrcode_img"] if qr else "",
        }
    except Exception as e:
        logger.error(f"[mock-subscribe] 失败: {e}")
        return {"success": False, "error": str(e)}


'''

if marker in c:
    # Insert before marker
    idx = c.find(marker)
    c = c[:idx] + new_endpoint + c[idx:]
    with open(path, 'w') as f:
        f.write(c)
    print('ENDPOINT_ADDED')
else:
    print('MARKER_NOT_FOUND')

import ast
try:
    ast.parse(c)
    print('SYNTAX_OK')
except SyntaxError as e:
    print(f'SYNTAX_ERROR: {e}')
