import re

with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'r') as f:
    content = f.read()

changes = 0

# === Fix 1: 加好友后首条消息（未绑定）===
old1 = '''        bind_msg = (
            "\U0001f4f1 您还未绑定手机号,请点击下方链接完成绑定:\\n\\n"
            f"{short_url}\\n\\n"
            "绑定手机号后即可使用全部功能 \U0001f389"
        )'''

new1 = '''        bind_msg = (
            "\U0001f99e 欢迎来到享客虾！\\n\\n"
            "我是你的 AI 创作伙伴，先绑定手机号，送你 15 天免费体验 \U0001f381\\n\\n"
            f"{short_url}\\n\\n"
            "绑定后你可以:\\n"
            "\U0001f3b5 AI 写歌 \\u00b7 说出你的故事，AI 为你谱曲\\n"
            "\U0001f49d AI 嗨卡 \\u00b7 照片+诗+歌，传心意\\n"
            "\U0001f916 智能聊天 \\u00b7 随叫随到\\n\\n"
            "\U0001f48e 15天后 \u00a59.9/月续费，体验期间全部功能开放"
        )'''

if old1 in content:
    content = content.replace(old1, new1, 1)
    changes += 1
    print('Fix 1: OK')
else:
    print('Fix 1: searching...')
    m = re.search(r'bind_msg = \(.*?您还未绑定.*?\)', content, re.DOTALL)
    if m:
        print('  Found at:', repr(m.group()[:80]))
    else:
        m = re.search(r'bind_msg = \(', content)
        if m:
            print('  bind_msg found. Showing context:')
            start = m.start()
            print(repr(content[start:start+300]))

# === Fix 2: 绑定成功后欢迎消息 ===
old2 = '        response = f"\u2705 绑定成功!欢迎你,{nickname} \U0001f389\\n\\n"\n        return {"success": True, "response": response}'

new2 = '''        response = (
            f"\U0001f389 绑定成功！欢迎你，{nickname} \U0001f389\\n\\n"
            "你现在可以开始 15 天的创作之旅了！\\n\\n"
            "\U0001f4a1 试试这样跟我聊：\\n"
            "\u2022 「帮我写首歌」\\u2014 AI 为你创作原创音乐\\n"
            "\u2022 「做张嗨卡」\\u2014 照片+诗+歌送给朋友\\n"
            "\u2022 「随便聊聊」\\u2014 我陪你说说话\\n\\n"
            "\U0001f514 体验到期前我会提醒你续费，仅 \u00a59.9/月"
        )
        return {"success": True, "response": response}'''

if old2 in content:
    content = content.replace(old2, new2, 1)
    changes += 1
    print('Fix 2: OK')
else:
    print('Fix 2: searching...')
    m = re.search(r'response = f.*?绑定成功.*?return.*?response\}', content, re.DOTALL)
    if m:
        print('  Found:', repr(m.group()[:100]))
    else:
        # Just search for the response line
        m = re.search(r'response = f.*绑定成功.*', content)
        if m:
            print('  Found response line:', repr(m.group()))

if changes > 0:
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.write(content)
    print(f'Written {changes} changes')
else:
    print('No changes made - need to investigate file content')
