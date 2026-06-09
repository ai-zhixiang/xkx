with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'r') as f:
    lines = f.readlines()

print('Lines around system_prompt:')
for i in range(max(0, 1208), min(len(lines), 1216)):
    print(f'{i+1}: {lines[i].rstrip()}')

print(f'\nTotal lines: {len(lines)}')
