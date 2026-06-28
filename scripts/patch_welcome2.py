import sys
sys.path.insert(0, '.')

with open('app/routes/bot_gateway.py') as f:
    lines = f.readlines()

changes = 0

# Fix 1: bind_msg
old_bind = ''.join(lines[1018:1024])
new_bind = (
    '        bind_msg = (\n'
    '            "\U0001f99e 欢迎来到享客虾！\\n\\n"\n'
    '            "我是你的 AI 创作伙伴，先绑定手机号，送你 15 天免费体验 \U0001f381\\n\\n"\n'
    '            f"{short_url}\\n\\n"\n'
    '            "绑定后你可以:\\n"\n'
    '            "\U0001f3b5 AI 写歌 \u00b7 说出你的故事，AI 为你谱曲\\n"\n'
    '            "\U0001f49d AI 嗨卡 \u00b7 照片+诗+歌，传心意\\n"\n'
    '            "\U0001f916 智能聊天 \u00b7 随叫随到\\n\\n"\n'
    '            "\U0001f48e 15天后 \u00a59.9/月续费，体验期间全部功能开放"\n'
    '        )\n'
)
lines[1018:1024] = [new_bind]
changes += 1
print(f'Fix 1: bind_msg patched ({len(old_bind)} chars -> {len(new_bind)} chars)')

# Fix 2: welcome message
old_welcome = ''.join(lines[1041:1044])
new_welcome = (
    '        response = (\n'
    '            f"\U0001f389 绑定成功！欢迎你，{nickname} \U0001f389\\n\\n"\n'
    '            "你现在可以开始 15 天的创作之旅了！\\n\\n"\n'
    '            "\U0001f4a1 试试这样跟我聊：\\n"\n'
    '            "\u2022 \u300c帮我写首歌\u300d\u2014 AI 为你创作原创音乐\\n"\n'
    '            "\u2022 \u300c做张嗨卡\u300d\u2014 照片+诗+歌送给朋友\\n"\n'
    '            "\u2022 \u300c随便聊聊\u300d\u2014 我陪你说说话\\n\\n"\n'
    '            "\U0001f514 体验到期前我会提醒你续费，仅 \u00a59.9/月"\n'
    '        )\n'
    '        return {"success": True, "response": response}\n'
)
lines[1041:1044] = [new_welcome]
changes += 1
print(f'Fix 2: welcome patched ({len(old_welcome)} chars -> {len(new_welcome)} chars)')

with open('app/routes/bot_gateway.py', 'w') as f:
    f.writelines(lines)

import py_compile
py_compile.compile('app/routes/bot_gateway.py', doraise=True)
print(f'Syntax OK. {changes} changes applied.')
