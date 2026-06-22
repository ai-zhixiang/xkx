#!/usr/bin/env python3
"""Remove the iLink binding check - make bot work immediately without phone binding."""
with open("/home/ubuntu/weclaw-1/app/routes/bot_gateway.py", "r") as f:
    lines = f.readlines()

# Find and replace the binding check block
new_lines = []
skip_until = -1
for i, line in enumerate(lines):
    if 'if not bound_info.get("bound"):' in line:
        # Found start of binding check - find the end (return statement + blank)
        skip_start = i
        skip_end = i
        for j in range(i, min(i + 15, len(lines))):
            if 'return {"success": True, "response": bind_msg}' in lines[j]:
                skip_end = j + 1  # include this line
                break
        # Also skip the blank line and "# 已绑定" comment after the block
        if skip_end < len(lines) and lines[skip_end].strip() == "":
            skip_end += 1
        if skip_end < len(lines) and "已绑定" in lines[skip_end]:
            skip_end += 1
            
        skip_until = skip_end
        new_lines.append('    # 跳过绑定验证 - 扫码即用\n')
        new_lines.append(f'    openid = bound_info.get("openid", channel_user_id) if bound_info.get("bound") else channel_user_id\n')
        new_lines.append(f'    nickname = bound_info.get("nickname", "")\n')
        new_lines.append(f'    welcomed = bound_info.get("welcomed", True)\n')
        new_lines.append(f'    user_account_id = bound_info.get("user_account_id")\n')
        continue
    
    if skip_until > 0 and i < skip_until:
        continue
    
    new_lines.append(line)

with open("/home/ubuntu/weclaw-1/app/routes/bot_gateway.py", "w") as f:
    f.writelines(new_lines)

print(f"Removed binding check (lines {skip_start+1}-{skip_until})")
