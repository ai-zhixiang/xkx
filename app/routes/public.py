from fastapi import APIRouter
router = APIRouter()
@router.get("/public/stub")
async def public_stub():
    return {"status": "stub"}
