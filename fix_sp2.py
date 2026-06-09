with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'r') as f:
    lines = f.readlines()

# Replace lines 1210-1213 (0-indexed: 1209-1212) with new system prompt
new_lines = [
    '    media_hint = ""\n',
    '    if media_path:\n',
    '        media_hint = f"\\n用户发来媒体文件: {media_path}"\n',
    '    \n',
    '    system_prompt = (\n',
    '        f"当前用户: {user_nickname or "铭道"} | OpenID: {(openid or "")[:16]}...{media_hint}\\n"\n',
    '        f"注意：你的 persona 和记忆已加载。用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。"\n',
    '    )\n',
]

lines[1209:1213] = new_lines

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
    f.writelines(lines)

print('DONE - replaced system_prompt block with media_hint')
