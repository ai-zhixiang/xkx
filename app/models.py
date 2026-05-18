"""
享客虾 — 数据库模型
个人AI秘书 · 订阅制 · 微信原生
"""
import os
from datetime import datetime, date
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean,
    DateTime, Date, Text, ForeignKey, Enum as SAEnum, JSON
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from dotenv import load_dotenv
import enum

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql+asyncpg://lucky:lucky_pass@localhost:5432/xiaolongxia')

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ===== 枚举 =====

class SubscriberStatus(str, enum.Enum):
    ACTIVE = 'active'         # 付费中
    EXPIRED = 'expired'       # 已到期
    CANCELLED = 'cancelled'   # 已取消


class OrderStatus(str, enum.Enum):
    PENDING = 'pending'
    PAID = 'paid'
    REFUNDED = 'refunded'


# ===== 订阅套餐 =====

class Plan(Base):
    __tablename__ = 'plans'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(30), nullable=False)
    price = Column(Integer, nullable=False)              # 月费（分）
    monthly_messages = Column(Integer, nullable=False)   # 月消息配额（0=无限）
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    subscribers = relationship('Subscriber', back_populates='plan')


# ===== 订阅用户 =====

class Subscriber(Base):
    __tablename__ = 'subscribers'

    id = Column(Integer, primary_key=True, autoincrement=True)
    openid = Column(String(128), unique=True, nullable=False, index=True)

    # QQ Bot 绑定
    qq_openid = Column(String(128), unique=True, nullable=True, index=True)

    # 用户信息（从微信 OAuth 获取）
    nickname = Column(String(64), default='')
    avatar_url = Column(String(256), default='')

    # 套餐
    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=False)
    plan = relationship('Plan', back_populates='subscribers')

    # 状态与时长
    status = Column(SAEnum(SubscriberStatus), default=SubscriberStatus.ACTIVE)
    started_at = Column(Date, nullable=False)
    expires_at = Column(Date, nullable=False)

    # 用量
    messages_used = Column(Integer, default=0)            # 本月已用
    messages_limit = Column(Integer, default=100)          # 本月配额（从plan同步）
    last_reset_at = Column(Date, nullable=True)            # 上次重置日
    total_messages = Column(Integer, default=0)            # 历史总消息

    # 时间
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship('ChatConversation', back_populates='subscriber')
    orders = relationship('SubOrder', back_populates='subscriber')


# ===== 对话记录 =====

class ChatConversation(Base):
    __tablename__ = 'chat_conversations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(Integer, ForeignKey('subscribers.id'), nullable=False)
    subscriber = relationship('Subscriber', back_populates='conversations')

    # 对话内容
    messages = Column(JSON, default=list)                  # [{role, content, ts}]
    message_count = Column(Integer, default=0)

    # 状态
    is_active = Column(Boolean, default=True)
    last_message_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


# ===== 订单 =====

class SubOrder(Base):
    __tablename__ = 'sub_orders'

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(Integer, ForeignKey('subscribers.id'), nullable=False)
    subscriber = relationship('Subscriber', back_populates='orders')

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=False)
    plan_name = Column(String(30))
    amount = Column(Integer, nullable=False)                # 分
    months = Column(Integer, default=1)
    status = Column(SAEnum(OrderStatus), default=OrderStatus.PENDING)

    payment_method = Column(String(20), default='wechat')
    new_expires_at = Column(Date)

    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime)


# ===== 管理员绑定 =====

class AdminBinding(Base):
    __tablename__ = 'admin_bindings'
    id = Column(Integer, primary_key=True, autoincrement=True)
    openid = Column(String(128), unique=True, nullable=False, index=True)
    nickname = Column(String(64), default='')
    is_active = Column(Boolean, default=True)
    bound_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, default=datetime.utcnow)


# ===== 通知 =====

class Notification(Base):
    __tablename__ = 'notifications'
    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(32), default='system')
    title = Column(String(128))
    content = Column(Text, default='')
    subscriber_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ===== QQ 绑定码 =====

class QQBindCode(Base):
    __tablename__ = 'qq_bind_codes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    qq_openid = Column(String(128), nullable=False, index=True)
    code = Column(String(8), nullable=False, unique=True, index=True)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ===== 数据库初始化 =====

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# ===== 种子数据 =====

SEED_PLANS = [
    {'name': '基础版', 'price': 990,   'monthly_messages': 500,  'sort_order': 0},
    {'name': '标准版', 'price': 1990,  'monthly_messages': 2000, 'sort_order': 1},
    {'name': '专业版', 'price': 19900, 'monthly_messages': 0,    'sort_order': 2, 'is_active': False},  # v0.5 上线
]
