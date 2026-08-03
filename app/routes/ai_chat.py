from fastapi import APIRouter
from fastapi.responses import HTMLResponse
router = APIRouter()

@router.get("/ai")
async def ai_chat():
    with open("/var/www/html/ai/index.html") as f:
        return HTMLResponse(f.read())

@router.post("/api/ai")
async def ai_chat_post(data: dict):
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.post("http://127.0.0.1:9101/api/message", json=data, timeout=60)
        return r.json()


@router.post("/api/message")
async def proxy_message(data: dict):
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.post("http://127.0.0.1:9101/api/message", json=data, timeout=60)
        return r.json()

