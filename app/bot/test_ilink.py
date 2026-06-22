import asyncio, json, time, secrets, struct, base64
import httpx

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
TOKEN = "acfb71544f8c@im.bot:0600006e1b733330722e8fa98c0554feac9b55"

def build_headers(token, body_len=0):
    uin = base64.b64encode(str(struct.unpack(">I", secrets.token_bytes(4))[0]).encode()).decode()
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (2 << 8) | 0),
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": uin,
    }
    if body_len:
        h["Content-Length"] = str(body_len)
    return h

async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        # get_updates (short poll, 5s)
        poll_body = json.dumps({
            "base_info": {"channel_version": "2.2.0"},
            "get_updates_buf": "",
            "sync_buf": "",
        }, separators=(",", ":"))
        h = build_headers(TOKEN, len(poll_body.encode()))
        r = await c.post(f"{ILINK_BASE_URL}/ilink/bot/getupdates", content=poll_body, headers=h)
        print(f"get_updates HTTP {r.status_code}")
        data = r.json()
        print(f"ret={data.get('ret')}, errcode={data.get('errcode')}")
        msgs = data.get("msgs", [])
        print(f"{len(msgs)} msgs")

        ctx = ""
        from_user = ""
        for m in msgs:
            ctx = m.get("context_token", "")
            from_user = m.get("from_user_id", "")
            text = ""
            for item in m.get("item_list", []):
                if item.get("type") == 1:
                    text = item.get("text_item", {}).get("text", "")
            print(f"  from={from_user[:20]} ctx={ctx[:16] if ctx else 'N/A'} text={text[:40]}")

        if ctx and from_user:
            print(f"\nSending reply with context_token...")
            msg = {
                "from_user_id": "",
                "to_user_id": from_user,
                "client_id": str(int(time.time() * 1000)),
                "message_type": 2,
                "message_state": 2,
                "context_token": ctx,
                "item_list": [{"type": 1, "text_item": {"text": "测试回复 OK ✅"}}],
            }
            body = json.dumps({"msg": msg, "base_info": {"channel_version": "2.2.0"}}, separators=(",", ":"))
            h2 = build_headers(TOKEN, len(body.encode()))
            r2 = await c.post(f"{ILINK_BASE_URL}/ilink/bot/sendmessage", content=body, headers=h2)
            print(f"send HTTP {r2.status_code}")
            print(f"send Raw: [{r2.text[:500]}]")
        else:
            print("\nNo context_token - trying send without it")
            msg = {
                "from_user_id": "",
                "to_user_id": "o9cq806n88EiZCsWOatm",
                "client_id": str(int(time.time() * 1000)),
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": "测试无context回复"}}],
            }
            body = json.dumps({"msg": msg, "base_info": {"channel_version": "2.2.0"}}, separators=(",", ":"))
            h3 = build_headers(TOKEN, len(body.encode()))
            r3 = await c.post(f"{ILINK_BASE_URL}/ilink/bot/sendmessage", content=body, headers=h3)
            print(f"send HTTP {r3.status_code}")
            print(f"send Raw: [{r3.text[:500]}]")

asyncio.run(main())
