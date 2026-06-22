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

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql+asyncpg://lucky:lucky_pass@localhost:5432/weclawd')

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ===== 枚举 =====

class SubscriberStatus(str, enum.Enum):
    ACTIVE = 'active'         # 付费中
    EXPIRED = 'expired'       # 已到期
    CANCELLED = 'cancelled'   # 已取消
    TRIAL = 'trial'           # 试用中
    VISITOR = 'visitor'       # 访客（未订阅，统一用户体系自动入库）


class OrderStatus(str, enum.Enum):
    PENDING = 'pending'
    PAID = 'paid'
    REFUNDED = 'refunded'


# ===== 订阅套餐 =====

class Plan(Base):
    __tablename__ = 'plans'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(30), nullable=False)
    price = Column(Integer, nullable=False)              # 售价（分）— 当前推广价
    original_price = Column(Integer, nullable=True)      # 原价（分）— 锚定价格，null=无折扣
    months = Column(Integer, default=1)                  # 时长（月）
    monthly_messages = Column(Integer, nullable=False)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

    subscribers = relationship('Subscriber', back_populates='plan')


# ===== 订阅用户 =====

class Subscriber(Base):
    __tablename__ = 'subscribers'

    id = Column(Integer, primary_key=True, autoincrement=True)
    openid = Column(String(128), unique=True, nullable=False, index=True)

    # QQ Bot 绑定
    qq_openid = Column(String(128), unique=True, nullable=True, index=True)

    # 统一用户体系：与 AI嗨卡/智享家 共享设备标识
    device_uuid = Column(String(64), nullable=True, index=True)

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

    # 试用标记
    trial_used = Column(Boolean, default=False)             # 是否已领取过试用

    # 时间
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

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
    last_message_at = Column(DateTime, default=datetime.now)
    created_at = Column(DateTime, default=datetime.now)


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

    out_trade_no = Column(String(64))        # 微信商户订单号
    transaction_id = Column(String(64))      # 微信支付订单号
    refund_status = Column(String(20))       # 退款状态: null/processing/success/failed
    refund_id = Column(String(64))           # 微信退款单号
    refund_amount = Column(Integer)          # 退款金额(分)

    created_at = Column(DateTime, default=datetime.now)
    paid_at = Column(DateTime)


# ===== 管理员绑定 =====

class AdminBinding(Base):
    __tablename__ = 'admin_bindings'
    id = Column(Integer, primary_key=True, autoincrement=True)
    openid = Column(String(128), unique=True, nullable=False, index=True)
    nickname = Column(String(64), default='')
    is_active = Column(Boolean, default=True)
    bound_at = Column(DateTime, default=datetime.now)
    last_login_at = Column(DateTime, default=datetime.now)


# ===== 通知 =====

class Notification(Base):
    __tablename__ = 'notifications'
    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(32), default='system')
    title = Column(String(128))
    content = Column(Text, default='')
    subscriber_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


# ===== QQ 绑定码 =====

class QQBindCode(Base):
    __tablename__ = 'qq_bind_codes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    qq_openid = Column(String(128), nullable=False, index=True)
    code = Column(String(8), nullable=False, unique=True, index=True)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class Document(Base):
    __tablename__ = 'documents'
    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(Integer, ForeignKey('subscribers.id'), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    subtitle = Column(String(200))
    summary = Column(String(500))
    category = Column(String(50), default='其他')
    file_path = Column(String(500))
    file_size = Column(Integer, default=0)
    content_text = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    is_deleted = Column(Boolean, default=False)

    subscriber = relationship('Subscriber', backref='documents')


# ===== 落地页访问统计 =====

class PageVisit(Base):
    __tablename__ = 'page_visits'

    id = Column(Integer, primary_key=True, autoincrement=True)
    openid = Column(String(128), nullable=True, index=True)   # null=匿名访客
    page = Column(String(64), default='landing')               # landing / pricing / etc
    source = Column(String(128), nullable=True)                 # utm_source / referrer
    ip = Column(String(45), nullable=True)
    ua = Column(String(256), nullable=True)
    converted = Column(Boolean, default=False)                  # 是否转化为订阅
    converted_at = Column(DateTime, nullable=True)
    subscriber_id = Column(Integer, ForeignKey('subscribers.id'), nullable=True)

    created_at = Column(DateTime, default=datetime.now)


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
    # 基础版（原价×10，一折推广）
    {'name': '基础月卡', 'price': 990,  'original_price': 9900,  'months': 1,  'monthly_messages': 500,  'sort_order': 0},
    {'name': '基础季卡', 'price': 2490, 'original_price': 24900, 'months': 3,  'monthly_messages': 500,  'sort_order': 1},
    {'name': '基础年卡', 'price': 7900, 'original_price': 79000, 'months': 12, 'monthly_messages': 500,  'sort_order': 2},
    # 标准版
    {'name': '标准月卡', 'price': 1990, 'original_price': 19900, 'months': 1,  'monthly_messages': 2000, 'sort_order': 3},
    {'name': '标准季卡', 'price': 4990, 'original_price': 49900, 'months': 3,  'monthly_messages': 2000, 'sort_order': 4},
    {'name': '标准年卡', 'price': 16800,'original_price': 168000,'months': 12, 'monthly_messages': 2000, 'sort_order': 5},
    # 专业版（暂未上线）
    {'name': '专业月卡', 'price': 19900,'original_price': 199000,'months': 1,  'monthly_messages': 0,    'sort_order': 6, 'is_active': False},
]
