from fastapi import APIRouter
router = APIRouter()
@router.get("/documents/stub")
async def documents_stub():
    return {"status": "stub"}
