with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'r') as f:
    lines = f.readlines()

print(f'Total lines: {len(lines)}')
print('Lines 1210-1222:')
for i in range(1209, min(1222, len(lines))):
    print(f'{i+1}: {lines[i].rstrip()}')

# Delete lines 1217-1219 (0-indexed: 1216-1218) - these are corrupted leftovers
# But first verify they're the garbage lines
if ')[:16]}' in lines[1216] and 'f\"' in lines[1217]:
    print('\nDeleting corrupted lines 1217-1219...')
    del lines[1216:1219]
    
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py', 'w') as f:
        f.writelines(lines)
    print('DONE')
    
    # Verify
    with open('/home/ubuntu/weclaw-1/app/routes/bot_gateway.py') as f2:
        lines2 = f2.readlines()
    print(f'New total lines: {len(lines2)}')
    for i in range(1209, min(1218, len(lines2))):
        print(f'{i+1}: {lines2[i].rstrip()}')
else:
    print('Unexpected content at lines 1217-1219, not deleting')
