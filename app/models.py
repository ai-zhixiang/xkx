"""享客虾 — SQLAlchemy 数据模型"""
import enum
from datetime import datetime, date

from sqlalchemy import (Column, Integer, String, Boolean, Date, DateTime,
                        BigInteger, Text, ForeignKey, JSON, Enum, create_engine,
                        UniqueConstraint, Index)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.orm import mapped_column

import os
import uuid

DATABASE_URL = os.getenv("DATABASE_URL",
    "postgresql+asyncpg://weclawd:weclawd_pass@localhost:5432/weclawd")


class Base(DeclarativeBase):
    pass


# ── Enums ──

class SubscriberStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    TRIAL = "TRIAL"
    VISITOR = "VISITOR"
    PENDING = "PENDING"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    REFUNDED = "REFUNDED"


# ── 套餐 Plan ──

class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(30), nullable=False)
    price = Column(Integer, nullable=False)
    original_price = Column(Integer, nullable=True)
    months = Column(Integer, nullable=True)
    monthly_messages = Column(Integer, nullable=False, default=0)
    sort_order = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


SEED_PLANS = [
    {"name": "基础月卡", "price": 9900, "months": 1, "monthly_messages": 0, "sort_order": 1, "original_price": 99000},
    {"name": "基础季卡", "price": 2490, "months": 3, "monthly_messages": 500, "sort_order": 2, "original_price": 24900},
    {"name": "基础年卡", "price": 99900, "months": 12, "monthly_messages": 500, "sort_order": 3, "original_price": 999000},
    {"name": "标准月卡", "price": 1990, "months": 1, "monthly_messages": 2000, "sort_order": 4, "original_price": 19900},
    {"name": "标准季卡", "price": 4990, "months": 3, "monthly_messages": 2000, "sort_order": 5, "original_price": 49900},
    {"name": "标准年卡", "price": 16800, "months": 12, "monthly_messages": 2000, "sort_order": 6, "original_price": 168000},
    {"name": "专业月卡", "price": 19900, "months": 1, "monthly_messages": 0, "sort_order": 7, "original_price": 199000},
]


# ── 订阅用户 Subscriber ──

class Subscriber(Base):
    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    openid = Column(String(128), nullable=False, unique=True, index=True)
    qq_openid = Column(String(128), unique=True, nullable=True)
    device_uuid = Column(String(64), index=True, nullable=True)
    nickname = Column(String(64), nullable=True)
    avatar_url = Column(String(256), nullable=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    status = Column(Enum(SubscriberStatus), default=SubscriberStatus.ACTIVE)
    started_at = Column(Date, nullable=False)
    expires_at = Column(Date, nullable=False)
    messages_used = Column(Integer, default=0)
    messages_limit = Column(Integer, default=0)
    last_reset_at = Column(Date, nullable=True)
    total_messages = Column(Integer, default=0)
    trial_used = Column(Boolean, default=False)
    phone = Column(String(32), unique=True, nullable=True)
    xiake_points = Column(Integer, nullable=False, default=3000)
    points_expires_at = Column(Date, nullable=True)
    total_points_consumed = Column(Integer, nullable=False, default=0)
    total_points_recharged = Column(Integer, nullable=False, default=0)
    disk_quota_bytes = Column(BigInteger, nullable=False, default=2_147_483_648)
    disk_used_bytes = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    conversations = relationship("ChatConversation", back_populates="subscriber")
    orders = relationship("SubOrder", back_populates="subscriber")


class SubOrder(Base):
    __tablename__ = "sub_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    plan_name = Column(String(30), nullable=True)
    amount = Column(Integer, nullable=False)
    months = Column(Integer, nullable=True)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    payment_method = Column(String(20), nullable=True)
    new_expires_at = Column(Date, nullable=True)
    out_trade_no = Column(String(64), nullable=True)
    transaction_id = Column(String(64), nullable=True)
    refund_status = Column(String(20), nullable=True)
    refund_id = Column(String(64), nullable=True)
    refund_amount = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    paid_at = Column(DateTime, nullable=True)

    subscriber = relationship("Subscriber", back_populates="orders")
    plan = relationship("Plan")


class PageVisit(Base):
    __tablename__ = "page_visits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    openid = Column(String(128), index=True, nullable=True)
    page = Column(String(64), nullable=True)
    source = Column(String(128), nullable=True)
    ip = Column(String(45), nullable=True)
    ua = Column(String(256), nullable=True)
    converted = Column(Boolean, default=False)
    converted_at = Column(DateTime, nullable=True)
    subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    messages = Column(JSON, nullable=True)
    message_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    last_message_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    subscriber = relationship("Subscriber", back_populates="conversations")


class BotAccount(Base):
    __tablename__ = "bot_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(String(128), nullable=False, unique=True)
    bot_token = Column(Text, nullable=True)
    user_id = Column(String(128), nullable=True)
    nickname = Column(String(128), nullable=True)
    is_active = Column(Boolean, default=True)
    platform = Column(String(32), default="weixin")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    backend = Column(String(50), default="hermes")
    svc_openid = Column(String(128), nullable=True)


class ChannelBinding(Base):
    __tablename__ = "channel_bindings"

    channel_type = Column(String(32), primary_key=True)
    channel_user_id = Column(String(128), primary_key=True)
    openid = Column(String(128), nullable=True)
    nickname = Column(String(128), nullable=True)
    phone = Column(String(32), nullable=True)
    user_account_id = Column(Integer, nullable=True)
    welcomed = Column(Boolean, default=False)
    bound_at = Column(DateTime, default=datetime.now)
    is_active = Column(Boolean, default=True)




# ── 微侠令 Order ──

class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(128), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="draft")
    source_text = Column(Text, nullable=True)
    source_channel = Column(String(20), nullable=True)
    execute_node = Column(String(50), nullable=True)
    node_config = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=True)
    progress = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    confirmed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_orders_user_status", "user_id", "status"),
        Index("idx_orders_created", "user_id", "created_at"),
        Index("idx_orders_status", "status"),
    )


class OrderChannel(Base):
    __tablename__ = "order_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(20), nullable=False)
    last_status = Column(String(20), nullable=False)
    notified_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("order_id", "channel"),
        Index("idx_oc_order", "order_id"),
    )


class BindSession(Base):
    __tablename__ = "bind_sessions"

    session_id = Column(String(32), primary_key=True)
    openid = Column(String(128), nullable=False)
    nickname = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.now)
    user_id = Column(String(128), nullable=True)  # iLink user_id, filled after scan
    bot_id = Column(String(128), nullable=True)   # iLink bot_id, filled after scan
    status = Column(String(20), nullable=False, default="pending")  # pending | bound


# ── 推广返佣 Referral ──

class ReferralCode(Base):
    """推广码"""
    __tablename__ = "referral_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    code = Column(String(20), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.now)

    subscriber = relationship("Subscriber")


class ReferralRelation(Base):
    """推广关系链"""
    __tablename__ = "referral_relations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    referee_openid = Column(String(128), nullable=False, unique=True, index=True)
    referrer_subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    referrer_openid = Column(String(128), nullable=False)
    level = Column(Integer, nullable=False, default=1)  # 1=一级, 2=二级
    referee_subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    referrer = relationship("Subscriber", foreign_keys=[referrer_subscriber_id])


class ReferralCommission(Base):
    """推广佣金记录"""
    __tablename__ = "referral_commissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    referrer_subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    referee_subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("sub_orders.id"), nullable=False)
    level = Column(Integer, nullable=False, default=1)  # 1=一级15%, 2=二级5%
    order_amount = Column(Integer, nullable=False)  # 订单金额（分）
    commission_rate = Column(Integer, nullable=False)  # 百分比（如 15 表示 15%）
    points_awarded = Column(Integer, nullable=False)  # 奖励虾点数
    status = Column(String(20), nullable=False, default="pending")  # pending / paid / cancelled
    settled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    referrer = relationship("Subscriber", foreign_keys=[referrer_subscriber_id])
    referee = relationship("Subscriber", foreign_keys=[referee_subscriber_id])
    order = relationship("SubOrder")


# ── 数据库初始化 ──

engine = None
async_engine = None
AsyncSessionLocal = None


async def init_db():
    global engine, async_engine, AsyncSessionLocal
    async_engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20)
    AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
'EOF'
