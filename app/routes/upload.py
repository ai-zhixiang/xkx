from fastapi import APIRouter
router = APIRouter()
@router.get("/upload/stub")
async def upload_stub():
    return {"status": "stub"}
