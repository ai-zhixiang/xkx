from fastapi import APIRouter
router = APIRouter()
@router.get("/auth/stub")
async def auth_stub():
    return {"status": "stub"}
