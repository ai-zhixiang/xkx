"""微侠令 — API 路由"""
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel


from app.services.order_service import (
    create_order as svc_create,
    submit_order as svc_submit,
    confirm_order as svc_confirm,
    reject_order as svc_reject,
    cancel_order as svc_cancel,
    complete_order as svc_complete,
    update_progress as svc_progress,
    list_orders as svc_list,
    list_all_orders as svc_list_all,
    get_order_detail as svc_detail,
    update_order as svc_update,
    delete_order as svc_delete,
    count_pending as svc_pending,
    search_orders as svc_search,
)

router = APIRouter(prefix="/api/orders", tags=["微侠令"])


# ── Request Models ──

class CreateOrderRequest(BaseModel):
    user_id: str
    title: str
    content: Optional[str] = None
    source_text: Optional[str] = None
    source_channel: Optional[str] = "weixin"


class ChannelRequest(BaseModel):
    channel: str = "weixin"


class CompleteOrderRequest(BaseModel):
    result: Optional[dict] = None
    error_message: Optional[str] = None


class UpdateOrderRequest(BaseModel):
    execute_node: Optional[str] = None
    node_config: Optional[dict] = None


class UpdateProgressRequest(BaseModel):
    progress: int


# ── Dependencies ──

async def get_session():
    from app.models import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session


# ── Routes ──

@router.post("")
async def create_order(
    data: CreateOrderRequest,
    session = Depends(get_session),
):
    """创建令草案"""
    try:
        order = await svc_create(
            session,
            user_id=data.user_id,
            title=data.title,
            content=data.content,
            source_text=data.source_text,
            source_channel=data.source_channel,
        )
        return {"code": 0, "data": order}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{order_id}/submit")
async def submit_order(
    order_id: UUID,
    session = Depends(get_session),
):
    """提交令（draft → pending）"""
    try:
        order = await svc_submit(session, order_id)
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/all")
async def list_all_orders(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session = Depends(get_session),
):
    """获取所有令（调度引擎专用）"""
    result = await svc_list_all(session, status, page, size)
    return {"code": 0, "data": result}


@router.get("/search")
async def search_orders(
    keyword: str = Query(...),
    user_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session = Depends(get_session),
):
    """按关键词搜索令"""
    result = await svc_search(session, keyword, user_id, page, size)
    return {"code": 0, "data": result}


@router.get("")
async def list_orders(
    user_id: str = Query(...),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session = Depends(get_session),
):
    """获取令列表"""
    result = await svc_list(session, user_id, status, page, size)
    return {"code": 0, "data": result}


@router.get("/pending-count")
async def pending_count(
    user_id: str = Query(...),
    session = Depends(get_session),
):
    """统计待确认令数量"""
    count = await svc_pending(session, user_id)
    return {"code": 0, "data": {"count": count}}


@router.get("/{order_id}")
async def get_order(
    order_id: UUID,
    session = Depends(get_session),
):
    """获取令详情"""
    try:
        order = await svc_detail(session, order_id)
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{order_id}/confirm")
async def confirm_order(
    order_id: UUID,
    data: ChannelRequest,
    session = Depends(get_session),
):
    """确认执行"""
    try:
        order = await svc_confirm(session, order_id, data.channel)
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{order_id}/reject")
async def reject_order(
    order_id: UUID,
    data: ChannelRequest,
    session = Depends(get_session),
):
    """驳回令"""
    try:
        order = await svc_reject(session, order_id, data.channel)
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{order_id}/cancel")
async def cancel_order(
    order_id: UUID,
    data: ChannelRequest,
    session = Depends(get_session),
):
    """取消执行"""
    try:
        order = await svc_cancel(session, order_id, data.channel)
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{order_id}/complete")
async def complete_order(
    order_id: UUID,
    data: CompleteOrderRequest,
    session = Depends(get_session),
):
    """完成令（系统内部调用）"""
    try:
        order = await svc_complete(
            session, order_id,
            result=data.result,
            error_message=data.error_message,
        )
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{order_id}/progress")
async def update_progress(
    order_id: UUID,
    data: UpdateProgressRequest,
    session = Depends(get_session),
):
    """更新执行进度（系统内部调用）"""
    try:
        order = await svc_progress(session, order_id, data.progress)
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{order_id}")
async def patch_order(
    order_id: UUID,
    data: UpdateOrderRequest,
    session = Depends(get_session),
):
    """更新令字段（调度引擎用）"""
    kwargs = {}
    if data.execute_node is not None:
        kwargs["execute_node"] = data.execute_node
    if data.node_config is not None:
        kwargs["node_config"] = data.node_config
    if not kwargs:
        raise HTTPException(status_code=400, detail="无更新字段")
    try:
        order = await svc_update(session, order_id, **kwargs)
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{order_id}")
async def delete_order(
    order_id: UUID,
    session = Depends(get_session),
):
    """删除令"""
    try:
        await svc_delete(session, order_id)
        return {"code": 0, "message": "已删除"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))