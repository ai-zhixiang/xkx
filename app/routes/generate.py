from fastapi import APIRouter
router = APIRouter()
@router.get("/generate/stub")
async def generate_stub():
    return {"status": "stub"}
