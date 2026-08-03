#!/usr/bin/env python3
"""Patch _background_hermes_result: add failure/error push notifications"""
import ast

path = "/home/ubuntu/weclaw-1/app/routes/bot_gateway.py"

with open(path) as f:
    code = f.read()

# 1. Patch 300s timeout case (non-200 response)
old1 = '''            logger.warning(f"[BG] Hermes 后台返回 {_r.status_code}")
            return'''

new1 = '''            logger.warning(f"[BG] Hermes 后台返回 {_r.status_code}")
            # 通知用户失败
            async with _hx.AsyncClient(timeout=10) as _hk:
                await _hk.post("http://127.0.0.1:9100/api/send", json={
                    "bot_id": bot_id, "to_user": user_id,
                    "text": "⚠️ 任务超时或服务异常（300s未完成），请重试或分段提问。",
                    "context_token": "",
                })
            return'''

# 2. Patch exception handler to push error notification
old2 = '''        logger.error(f"[BG] 后台任务失败: {e}")'''

new2 = '''        logger.error(f"[BG] 后台任务失败: {e}")
        # 通知用户异常
        try:
            async with _hx.AsyncClient(timeout=10) as _hk:
                await _hk.post("http://127.0.0.1:9100/api/send", json={
                    "bot_id": bot_id, "to_user": user_id,
                    "text": f"\u26a0\ufe0f \u5904\u7406\u5f02\u5e38: {str(e)[:80]}。\u8bf7\u91cd\u8bd5\u3002",
                    "context_token": "",
                })
        except Exception:
            pass'''

changes = 0
if old1 in code:
    code = code.replace(old1, new1, 1)
    print("  ✅ 超时失败通知已添加")
    changes += 1
else:
    print("  ⚠️ 未找到超时处理块")

if old2 in code:
    code = code.replace(old2, new2, 1)
    print("  ✅ 异常通知已添加")
    changes += 1
else:
    print("  ⚠️ 未找到异常处理")

if changes == 0:
    print("  ❌ 无变更，退出")
    exit(1)

# Verify AST
try:
    ast.parse(code)
    print("  ✅ AST 语法验证通过")
except SyntaxError as e:
    print(f"  ❌ 语法错误: {e}")
    exit(1)

with open(path, "w") as f:
    f.write(code)
print(f"  ✅ {path} 已更新")
