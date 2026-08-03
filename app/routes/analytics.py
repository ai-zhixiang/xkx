from fastapi import APIRouter
router = APIRouter()
@router.get("/analytics/stub")
async def analytics_stub():
    return {"status": "stub"}
