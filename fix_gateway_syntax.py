# Fix the f-string syntax error in bot_gateway.py
# The issue: f-string with nested double quotes "铭道" 
# Fix: use string concatenation instead

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'r') as f:
    data = f.read()

# Find the problematic block
old = 'media_hint = ""\n    if media_path:\n        media_hint = f"\\n用户发来媒体文件: {media_path}"\n    \n    system_prompt = (\n        f"当前用户: {user_nickname or \\"铭道\\"} | OpenID: {(openid or \\"\\")[:16]}...{media_hint}\\n"\n        f"注意：你的 persona 和记忆已加载。用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。"'

# Alternative: find by substring and use exact bytes
idx = data.find('media_hint = ""')
if idx >= 0:
    # Read the block from that point
    block_end = data.find(')', idx) + 1
    block = data[idx:block_end]
    print(f'Found block at {idx}, {len(block)} chars')
    print(repr(block))
    
    new_block = '''media_hint = ""
    if media_path:
        media_hint = "\\n用户发来媒体文件: {mpath}".format(mpath=media_path)
    
    system_prompt = (
        "当前用户: " + (user_nickname or "铭道") + " | OpenID: " + (openid or "")[:16] + "..." + media_hint + "\\n"
        "注意：你的 persona 和记忆已加载。用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。"
    )'''
    
    data = data[:idx] + new_block + data[block_end:]
    
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(data)
    print('DONE')
else:
    print('NOT FOUND')
