from fastapi import APIRouter
router = APIRouter()
@router.get("/menu/stub")
async def menu_stub():
    return {"status": "stub"}
