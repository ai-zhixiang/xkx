#!/usr/bin/env python3
"""Modify bot_gateway.py to add intelligent routing: L1/L2→MD-1, L3/media→Bear2"""
import re

path = "/home/ubuntu/weclaw-1/app/routes/bot_gateway.py"

with open(path, "r") as f:
    content = f.read()

old = '''    hermes_url = "http://127.0.0.1:8642/v1/chat/completions"
    session_key = user_account_id or openid
    bot_prefix = bot_id + ":" if bot_id else "xiakexia:"
    session_id = bot_prefix + str(session_key) if session_key else ""

    # ── L0-L3 消息分级调度 ──
    msg_level = classify_message_level(content)
    if msg_level == 1:
        use_session_id = ""
        use_max_tokens = 128
        logger.info("📊 [L1] %s: %s", (openid or "?")[:12], content[:30])
    elif msg_level == 3:
        use_session_id = session_id
        use_max_tokens = 2048
        logger.info("📊 [L3] %s: %s", (openid or "?")[:12], content[:30])
    else:
        use_session_id = session_id
        use_max_tokens = 512
        logger.info("📊 [L2] %s: %s", (openid or "?")[:12], content[:30])'''

new = '''    session_key = user_account_id or openid
    bot_prefix = bot_id + ":" if bot_id else "xiakexia:"
    session_id = bot_prefix + str(session_key) if session_key else ""

    # ── 智能路由: L1/L2→MD-1(轻量稳定), L3/媒体→熊二(重计算) ──
    msg_level = classify_message_level(content)
    has_media = bool(media_path) and content.strip() in ("", "[图片]", "[视频]", "[语音]", "[文件]")
    if msg_level == 1:
        use_session_id = ""
        use_max_tokens = 128
        target = "md1"
        logger.info("📊 [L1] %s: %s", (openid or "?")[:12], content[:30])
    elif msg_level == 3 or has_media:
        use_session_id = session_id
        use_max_tokens = 2048
        target = "bear2"
        logger.info("📊 [L3/重] %s: %s (→熊二)", (openid or "?")[:12], content[:30])
    else:
        use_session_id = session_id
        use_max_tokens = 512
        target = "md1"
        logger.info("📊 [L2] %s: %s", (openid or "?")[:12], content[:30])

    if target == "bear2":
        hermes_url = "http://124.222.215.111/hermes-bridge/v1/chat/completions"
        hermes_api_key = "sk-2e4371922001435fb37b0a022f208121"
        cb_timeout = 600
    else:
        hermes_url = "http://127.0.0.1:8642/v1/chat/completions"
        hermes_api_key = "sk-16dadc6b0ca6a040248c0790d158bed4323d4185c005368359d3791a5bf36390"
        cb_timeout = _HERMES_TIMEOUTS.get(msg_level, 120)'''

if old in content:
    content = content.replace(old, new, 1)
    with open(path, "w") as f:
        f.write(content)
    print("PATCHED OK")
else:
    print("ERROR: old string not found")
    # Debug: find nearby text
    idx = content.find("hermes_url")
    print(f"Found hermes_url at position {idx}")
    print(content[idx:idx+800])
