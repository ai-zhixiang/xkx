#!/usr/bin/env python3
"""Patch bot_gateway.py: add mock payment + QR code flow for bound users"""
path = '/home/ubuntu/weclaw-1/app/routes/bot_gateway.py'
with open(path) as f:
    c = f.read()

old_marker = '订阅检查：已绑账号直接走 Hermes，否则引导绑定手机'
new_marker = '订阅检查：已绑账号 → 检查订阅，未绑定 → 引导绑定手机'

# Find exact block boundaries
idx = c.find(old_marker)
if idx < 0:
    print('MARKER NOT FOUND')
    exit(1)

# Find the start of the line containing the marker
line_start = c.rfind('\n', 0, idx) + 1

# Find the end of the _call_hermes line
end_marker = 'return await _call_hermes(content, user_id, user_nickname, openid, user_account_id, media_path, bot_id)'
end_idx = c.find(end_marker, line_start)
if end_idx < 0:
    print('END MARKER NOT FOUND')
    exit(1)
line_end = c.find('\n', end_idx) + 1

old_block = c[line_start:line_end]

new_block = """    # 订阅检查：已绑账号 → 检查订阅，未绑定 → 引导绑定手机
    if backend != "deepseek" and not user_account_id:
        return "🔑 请先绑定手机号。\\n\\n发送「绑定手机138xxxxxxxx」获取验证码，绑定后即可开通会员。"

    # 已绑手机 → 检查订阅状态 / mock 开通
    if backend != "deepseek" and user_account_id:
        import re as _re2
        _stripped = _re2.sub(r'[,。!?、;:\\"\'\\s]', '', content).lower()
        if _stripped in ("开通", "mock", "开通会员"):
            # Mock 支付 → 写入订阅 + 生成二维码
            try:
                from app.models import AsyncSessionLocal as _asf
                from sqlalchemy import text as _t
                async with _asf() as _s:
                    import datetime
                    await _s.execute(
                        _t("INSERT INTO subscribers (openid, status, plan, expires_at, created_at) "
                           "VALUES (:oid, 'ACTIVE', 'mock', :exp, NOW()) "
                           "ON CONFLICT (openid) DO UPDATE SET status='ACTIVE', expires_at=:exp2"),
                        {"oid": openid or user_id,
                         "exp": datetime.date.today() + datetime.timedelta(days=30),
                         "exp2": datetime.date.today() + datetime.timedelta(days=30)},
                    )
                    await _s.commit()
            except Exception:
                pass
            qr_info = await _fetch_qrcode()
            if qr_info:
                return (
                    "✅ Mock 支付成功！已开通 30 天会员 🎉\\n\\n"
                    "用微信扫描下方二维码，添加你的专属 Bot：\\n\\n"
                    f"🔗 {qr_info['qrcode_url']}\\n\\n"
                    "扫描后 Bot 会自动出现在你的微信联系人中 🫡"
                )
            else:
                return "❌ 二维码生成失败，请稍后再试"
        try:
            from app.models import AsyncSessionLocal as _asf
            from sqlalchemy import text as _t
            async with _asf() as _s:
                row = await _s.execute(
                    _t("SELECT status, expires_at FROM subscribers WHERE openid = :oid AND status = 'ACTIVE'"),
                    {"oid": openid or user_id},
                )
                sub = row.fetchone()
                if not sub or sub[1] < datetime.datetime.now().date():
                    return (
                        "🦞 欢迎来到享客虾！\\n\\n"
                        "已绑定手机，但还未开通会员。\\n"
                        "回复「开通」使用 Mock 支付体验完整流程 🎁"
                    )
        except Exception:
            pass

    # 路由
    if backend == "deepseek":
        return await _call_deepseek(content, user_nickname)
    else:
        return await _call_hermes(content, user_id, user_nickname, openid, user_account_id, media_path, bot_id)
"""

c = c[:line_start] + new_block + c[line_end:]
with open(path, 'w') as f:
    f.write(c)

# Verify
import ast
try:
    ast.parse(c)
    print('PATCHED_OK')
except SyntaxError as e:
    print(f'SYNTAX_ERROR: {e}')
