#!/usr/bin/env python3
"""Fix: skip notifyStop warmup for brand new bot sessions (no sync_buf)."""
with open("/home/ubuntu/weclaw-1/app/bot/unified_connector.py", "r") as f:
    code = f.read()

old = """    # ── Layer 2: 启动预热 — 先 notifyStop 清残留 ──
    if await notify_stop(token):
        log.info(\"  预热 notifyStop ✓\")
    await asyncio.sleep(2)"""

new = """    # ── Layer 2: 启动预热 — 仅已有 session 才 notifyStop（清残留）
    # 新 Bot（无历史 sync_buf）跳过，避免杀死刚扫码生成的 session
    has_old_session = bool(load_sync_buf(bot.bot_id))
    if has_old_session:
        if await notify_stop(token):
            log.info(\"  预热 notifyStop ✓\")
        await asyncio.sleep(2)
    else:
        log.info(\"  新 Bot 跳过 notifyStop（直接 notifyStart）\")"""

if old in code:
    code = code.replace(old, new, 1)
    with open("/home/ubuntu/weclaw-1/app/bot/unified_connector.py", "w") as f:
        f.write(code)
    import py_compile
    py_compile.compile("/home/ubuntu/weclaw-1/app/bot/unified_connector.py", doraise=True)
    print("FIX APPLIED - syntax OK")
else:
    print("ERROR: old text not found!")
