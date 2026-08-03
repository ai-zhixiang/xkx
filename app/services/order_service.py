"""微侠令 — 业务逻辑层"""
import uuid
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from sqlalchemy import select, func, update, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderChannel

# 所有通道列表（用于同步）
ALL_CHANNELS = ["weixin", "qq", "app", "yuanbao"]


async def create_order(
    session: AsyncSession,
    user_id: str,
    title: str,
    content: Optional[str] = None,
    source_text: Optional[str] = None,
    source_channel: Optional[str] = None,
    channels: Optional[List[str]] = None,
) -> dict:
    """创建令草案（draft 态），同步到各通道"""
    order = Order(
        id=uuid.uuid4(),
        user_id=user_id,
        title=title,
        content=content,
        status="draft",
        source_text=source_text,
        source_channel=source_channel,
        progress=0,
    )
    session.add(order)
    await session.flush()

    # 同步到各通道
    target_channels = channels or ALL_CHANNELS
    for ch in target_channels:
        oc = OrderChannel(
            order_id=order.id,
            channel=ch,
            last_status="draft",
            notified_at=datetime.now(),
        )
        session.add(oc)

    await session.commit()
    return _order_to_dict(order)


async def submit_order(
    session: AsyncSession,
    order_id: UUID,
) -> dict:
    """提交令：draft → pending（展示给用户等待确认）"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")
    if order.status != "draft":
        raise ValueError(f"当前状态 {order.status}，无法提交")

    order.status = "pending"
    order.updated_at = datetime.now()
    await session.execute(
        update(OrderChannel)
        .where(OrderChannel.order_id == order_id)
        .values(last_status="pending", notified_at=datetime.now())
    )
    await session.commit()
    return _order_to_dict(order)


async def confirm_order(
    session: AsyncSession,
    order_id: UUID,
    channel: str,
) -> dict:
    """确认执行：pending → executing"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")
    if order.status not in ("pending", "executing"):
        raise ValueError(f"当前状态 {order.status}，无法确认执行")

    if order.status == "pending":
        order.status = "executing"
        order.confirmed_at = datetime.now()
        order.updated_at = datetime.now()

    # 更新该通道的同步状态
    await session.execute(
        update(OrderChannel)
        .where(
            and_(
                OrderChannel.order_id == order_id,
                OrderChannel.channel == channel,
            )
        )
        .values(last_status="executing", notified_at=datetime.now())
    )
    await session.commit()
    return _order_to_dict(order)


async def reject_order(
    session: AsyncSession,
    order_id: UUID,
    channel: str,
) -> dict:
    """驳回：pending → rejected"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")
    if order.status != "pending":
        raise ValueError(f"当前状态 {order.status}，无法驳回")

    order.status = "rejected"
    order.updated_at = datetime.now()
    await session.execute(
        update(OrderChannel)
        .where(OrderChannel.order_id == order_id)
        .values(last_status="rejected", notified_at=datetime.now())
    )
    await session.commit()
    return _order_to_dict(order)


async def cancel_order(
    session: AsyncSession,
    order_id: UUID,
    channel: str,
) -> dict:
    """取消执行：executing → cancelled"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")
    if order.status != "executing":
        raise ValueError(f"当前状态 {order.status}，无法取消")

    order.status = "cancelled"
    order.updated_at = datetime.now()
    await session.execute(
        update(OrderChannel)
        .where(OrderChannel.order_id == order_id)
        .values(last_status="cancelled", notified_at=datetime.now())
    )
    await session.commit()
    return _order_to_dict(order)


async def complete_order(
    session: AsyncSession,
    order_id: UUID,
    result: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> dict:
    """完成执行：executing → completed/failed"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")
    if order.status != "executing":
        raise ValueError(f"当前状态 {order.status}，无法完成")

    if error_message:
        order.status = "failed"
        order.error_message = error_message
    else:
        order.status = "completed"
        order.result = result or {}
    order.progress = 100
    order.completed_at = datetime.now()
    order.updated_at = datetime.now()

    new_status = order.status
    await session.execute(
        update(OrderChannel)
        .where(OrderChannel.order_id == order_id)
        .values(last_status=new_status, notified_at=datetime.now())
    )
    await session.commit()
    return _order_to_dict(order)


async def update_progress(
    session: AsyncSession,
    order_id: UUID,
    progress: int,
) -> dict:
    """更新执行进度"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")

    order.progress = progress
    order.updated_at = datetime.now()
    await session.commit()
    return _order_to_dict(order)


async def search_orders(
    session: AsyncSession,
    keyword: str,
    user_id: Optional[str] = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """按关键词搜索令（标题/内容/源文本），可选按 user_id 过滤"""
    pattern = f"%{keyword}%"
    query = select(Order).where(
        or_(
            Order.title.ilike(pattern),
            Order.content.ilike(pattern),
            Order.source_text.ilike(pattern),
        )
    )
    if user_id:
        query = query.where(Order.user_id == user_id)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_q)).scalar() or 0

    offset = (page - 1) * size
    query = query.order_by(Order.created_at.desc()).offset(offset).limit(size)
    result = await session.execute(query)
    orders = result.scalars().all()

    return {
        "items": [_order_to_dict(o) for o in orders],
        "total": total,
        "page": page,
        "size": size,
    }


async def list_all_orders(
    session: AsyncSession,
    status: Optional[str] = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """查询所有令（调度引擎用，不限制 user_id）"""
    query = select(Order)
    if status:
        query = query.where(Order.status == status)
    count_q = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_q)).scalar() or 0
    offset = (page - 1) * size
    query = query.order_by(Order.created_at.desc()).offset(offset).limit(size)
    result = await session.execute(query)
    orders = result.scalars().all()
    return {
        "items": [_order_to_dict(o) for o in orders],
        "total": total,
        "page": page,
        "size": size,
    }


async def list_orders(
    session: AsyncSession,
    user_id: str,
    status: Optional[str] = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """查询令列表"""
    query = select(Order).where(Order.user_id == user_id)

    if status:
        query = query.where(Order.status == status)

    # 总数
    count_q = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_q)).scalar() or 0

    # 分页
    offset = (page - 1) * size
    query = query.order_by(Order.created_at.desc()).offset(offset).limit(size)
    result = await session.execute(query)
    orders = result.scalars().all()

    return {
        "items": [_order_to_dict(o) for o in orders],
        "total": total,
        "page": page,
        "size": size,
    }


async def get_order_detail(
    session: AsyncSession,
    order_id: UUID,
) -> dict:
    """获取令详情（含通道同步状态）"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")

    d = _order_to_dict(order)

    # 附上通道状态
    ch_result = await session.execute(
        select(OrderChannel).where(OrderChannel.order_id == order_id)
    )
    channels = ch_result.scalars().all()
    d["channels"] = [
        {
            "channel": c.channel,
            "last_status": c.last_status,
            "notified_at": c.notified_at.isoformat() if c.notified_at else None,
        }
        for c in channels
    ]

    return d


async def delete_order(
    session: AsyncSession,
    order_id: UUID,
) -> None:
    """删除令"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")
    await session.delete(order)
    await session.commit()


async def count_pending(
    session: AsyncSession,
    user_id: str,
) -> int:
    """统计待确认令数量"""
    result = await session.execute(
        select(func.count())
        .select_from(Order)
        .where(
            and_(
                Order.user_id == user_id,
                Order.status == "pending",
            )
        )
    )
    return result.scalar() or 0


async def update_order(
    session: AsyncSession,
    order_id: UUID,
    **kwargs,
) -> dict:
    """更新令字段（通用）"""
    order = await _get_order(session, order_id)
    if order is None:
        raise ValueError("令不存在")
    for key, value in kwargs.items():
        if hasattr(order, key):
            setattr(order, key, value)
    order.updated_at = datetime.now()
    await session.commit()
    return _order_to_dict(order)


# ── 内部函数 ──


async def _get_order(session: AsyncSession, order_id: UUID) -> Order:
    result = await session.execute(select(Order).where(Order.id == order_id))
    return result.scalar_one_or_none()


def _order_to_dict(order: Order) -> dict:
    return {
        "id": str(order.id),
        "user_id": order.user_id,
        "title": order.title,
        "content": order.content,
        "status": order.status,
        "source_text": order.source_text,
        "source_channel": order.source_channel,
        "execute_node": order.execute_node,
        "node_config": order.node_config,
        "result": order.result,
        "error_message": order.error_message,
        "parent_id": str(order.parent_id) if order.parent_id else None,
        "progress": order.progress,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "confirmed_at": order.confirmed_at.isoformat() if order.confirmed_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }