import re

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f:
    c = f.read()

# Find the bind_verify_code return messages
# Replace "绑定成功，赠送15天体验会员" with logic that checks if already bound
old1 = '''            await _s.commit()
            logger.info(f\"[Bind] 手机 {phone} 绑定到 user_account {acct_id} + 15天体验会员\")
            return {\"success\": True, \"message\": \"\\u7ed1\\u5b9a\\u6210\\u529f\\uff0c\\u8d60\\u900115\\u5929\\u4f53\\u9a8c\\u4f1a\\u5458 \\ud83c\\udf89\"}'''

new1 = '''            await _s.commit()
            is_new = not existing
            logger.info(f\"[Bind] 手机 {phone} -> user_account {acct_id} ({\\\"新\\\" if is_new else \\\"已有\\\"})\")
            msg = \"绑定成功！赠送15天体验会员 🎉\" if is_new else \"手机号已绑定，欢迎回来 🎉\"
            return {\"success\": True, \"message\": msg, \"is_new\": is_new}'''

# Use different approach - find and replace exact line
old_msg = '            return {"success": True, "message": "绑定成功，赠送15天体验会员 🎉"}'

# We need to find the full context
idx = c.find('return {"success": True, "message": "')
if idx > 0:
    # Show the context
    start = max(0, idx - 100)
    end = min(len(c), idx + 100)
    print(f'Found at {idx}:')
    print(repr(c[start:end]))
    
    # Replace the message
    old_msg_full = 'return {"success": True, "message": "绑定成功，赠送15天体验会员 🎉"}'
    new_msg_full = 'return {"success": True, "message": msg, "is_new": is_new}'
    c = c.replace(old_msg_full, new_msg_full, 1)
    if old_msg_full not in c:
        print('Message fix: OK')
    
    # Add is_new detection before the return
    old_code = '''            await _s.commit()
            logger.info(f\"[Bind] 手机 {phone} 绑定到 user_account {acct_id} + 15天体验会员\")'''
    new_code = '''            await _s.commit()
            is_new = existing is None
            logger.info(f\"[Bind] 手机 {phone} -> user_account {acct_id} ({\\\"新\\\" if is_new else \\\"已有\\\"})\")'''
    c = c.replace(old_code, new_code, 1)
    if old_code not in c:
        print('is_new detection: OK')
    
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(c)
    
    import py_compile
    py_compile.compile('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', doraise=True)
    print('Syntax OK')
else:
    print('Not found')
