from fastapi import APIRouter
router = APIRouter()
@router.get("/wechat/stub")
async def wechat_stub():
    return {"status": "stub"}
