#!/usr/bin/env python3
"""Add mock-subscribe endpoint to bot_gateway.py"""
with open("/home/ubuntu/weclaw-1/app/routes/bot_gateway.py", "r") as f:
    c = f.read()

insert = '''
@router.post("/mock-subscribe")
async def mock_subscribe(data: dict):
    """模拟开通 - 返回二维码"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode",
                json={"bot_type": 3},
                headers={"Content-Type": "application/json", "iLink-App-Id": "bot", "iLink-App-ClientVersion": str((2 << 16) | (2 << 8) | 0)},
            )
            d = r.json()
            qv = d.get("qrcode", "")
            lu = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={qv}&bot_type=3"
            return {"success": True, "message": "开通成功！", "qrcode_url": lu, "qrcode_value": qv, "bot_alive": False, "bot_id": data.get("bot_id", ""), "liteapp_url": lu}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, detail=f"生成二维码失败: {e}")

'''

old = '@router.get("/paused")'
if old in c:
    c = c.replace(old, insert + old, 1)
    print("Added before /paused")
else:
    old = '@router.get("/qrcode")'
    if old in c:
        c = c.replace(old, insert + old, 1)
        print("Added before /qrcode")
    else:
        print("ERROR: anchor not found")

with open("/home/ubuntu/weclaw-1/app/routes/bot_gateway.py", "w") as f:
    f.write(c)
compile(c, "bot_gateway.py", "exec")
print("Syntax OK")
