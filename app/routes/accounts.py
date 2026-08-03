"""用户账户 — API 路由
手机号绑定 bot → 数据打通共享
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import asyncpg
import os

DATABASE_URL = "postgresql://lucky:lucky_pass@localhost:5432/weclawd"
SHARED_DIR = "/home/ubuntu/weclaw-files/shared"

router = APIRouter(prefix="/api/accounts", tags=["用户账户"])


class BindRequest(BaseModel):
    bot_id: str
    phone: str
    channel_user_id: Optional[str] = None
    nickname: Optional[str] = None


class UnbindRequest(BaseModel):
    bot_id: str


class FindRequest(BaseModel):
    phone: str
    another_bot_id: Optional[str] = None


# ── 绑定手机号 ──

@router.post("/bind")
async def bind_phone(data: BindRequest):
    """绑定 bot 到手机号"""
    phone = data.phone.strip()
    if not phone or len(phone) < 5:
        raise HTTPException(400, "手机号格式不正确")

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # 检查该 bot 是否已被其他手机绑定
        row = await conn.fetchrow(
            "SELECT phone FROM user_accounts WHERE $1 = ANY(bot_ids)",
            data.bot_id
        )
        if row:
            if row["phone"] == phone:
                return {"status": "ok", "message": "该 bot 已绑定到此手机号", "phone": phone}
            raise HTTPException(400, "该 bot 已被其他手机号绑定，请先解绑")

        # 插入或追加 bot
        result = await conn.fetchrow(
            "UPDATE user_accounts SET bot_ids = array_append(bot_ids, $1), updated_at = NOW() "
            "WHERE phone = $2 AND NOT ($1 = ANY(bot_ids)) RETURNING phone, bot_ids",
            data.bot_id, phone
        )
        if not result:
            # 新手机号
            result = await conn.fetchrow(
                "INSERT INTO user_accounts (phone, bot_ids, created_at, updated_at) "
                "VALUES ($1, ARRAY[$2], NOW(), NOW()) RETURNING phone, bot_ids",
                phone, data.bot_id
            )

        # 更新 channel_bindings 的 phone 字段
        await conn.execute(
            "UPDATE channel_bindings SET phone = $1 "
            "WHERE channel_user_id LIKE $2 AND (phone IS NULL OR phone != $1)",
            phone, data.bot_id + "%"
        )

        # 创建共享目录
        phone_dir = phone.replace("+", "").replace("-", "")
        shared_path = os.path.join(SHARED_DIR, phone_dir)
        os.makedirs(shared_path, exist_ok=True)

        bot_count = len(result["bot_ids"])
        # Push notification to user in WeChat
        try:
            import httpx
            mask = phone[:3] + "****" + phone[-4:]
            to_user = data.channel_user_id or (data.bot_id.split("@")[0] + "@im.wechat")
            bot_id_clean = data.bot_id.split("@")[0]
            push_text = f"✅ 绑定成功！手机号 {mask} 已关联 {bot_count} 个 bot\n\n"
            push_text += f"📱 绑定后好处：\n"
            push_text += f"1️⃣ 同一手机号下的 bot 文件互通\n"
            push_text += f"2️⃣ 换新 bot 后绑定原手机号自动恢复数据"
            httpx.post(
                "http://127.0.0.1:9100/api/send",
                json={"bot_id": bot_id_clean, "to_user": to_user, "text": push_text},
                timeout=5
            )
        except Exception:
            pass
        return {
            "status": "ok",
            "message": f"绑定成功！已关联 {bot_count} 个 bot",
            "phone": phone,
            "bot_ids": result["bot_ids"]
        }
    finally:
        await conn.close()


# ── 解绑 bot ──

@router.post("/unbind")
async def unbind_bot(data: UnbindRequest):
    """从手机号解绑 bot"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT phone, bot_ids FROM user_accounts WHERE $1 = ANY(bot_ids)",
            data.bot_id
        )
        if not row:
            raise HTTPException(404, "该 bot 未绑定任何手机号")

        new_ids = [b for b in row["bot_ids"] if b != data.bot_id]
        if not new_ids:
            await conn.execute("DELETE FROM user_accounts WHERE phone = $1", row["phone"])
            return {"status": "ok", "message": "已解绑，账户已删除"}
        else:
            await conn.execute(
                "UPDATE user_accounts SET bot_ids = $1, updated_at = NOW() WHERE phone = $2",
                new_ids, row["phone"]
            )
            return {"status": "ok", "message": f"已解绑，剩余 {len(new_ids)} 个 bot", "bot_ids": new_ids}
    finally:
        await conn.close()


# ── 查询手机号信息 ──

@router.get("/info")
async def account_info(phone: str = Query(..., description="手机号")):
    """查询手机号绑定的所有 bot"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow("SELECT * FROM user_accounts WHERE phone = $1", phone)
        if not row:
            raise HTTPException(404, "该手机号未绑定任何 bot")
        return {
            "phone": row["phone"],
            "bot_ids": row["bot_ids"],
            "bot_count": len(row["bot_ids"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"])
        }
    finally:
        await conn.close()


# ── 通过 bot 查找手机号 ──

@router.get("/find")
async def find_by_bot(bot_id: str = Query(..., description="bot_id")):
    """根据 bot 查找绑定的手机号和关联的其他 bot"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT phone, bot_ids FROM user_accounts WHERE $1 = ANY(bot_ids)",
            bot_id
        )
        if not row:
            raise HTTPException(404, "该 bot 未绑定手机号")
        return {
            "phone": row["phone"],
            "bot_ids": row["bot_ids"],
            "other_bots": [b for b in row["bot_ids"] if b != bot_id]
        }
    finally:
        await conn.close()


# ── 迁移 bot ──

@router.post("/migrate")
async def migrate_bot(data: FindRequest):
    """旧 bot 丢失 → 新 bot 绑定到同一手机号"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow("SELECT phone, bot_ids FROM user_accounts WHERE phone = $1", data.phone)
        if not row:
            raise HTTPException(404, "手机号未绑定任何 bot")

        if not data.another_bot_id:
            return {
                "phone": row["phone"],
                "existing_bots": row["bot_ids"],
                "message": "请提供 another_bot_id 以绑定新 bot"
            }

        new_id = data.another_bot_id
        if new_id in row["bot_ids"]:
            return {"status": "ok", "message": "该 bot 已在绑定列表中"}

        new_ids = row["bot_ids"] + [new_id]
        await conn.execute(
            "UPDATE user_accounts SET bot_ids = $1, updated_at = NOW() WHERE phone = $2",
            new_ids, data.phone
        )
        return {
            "status": "ok",
            "message": f"新 bot 已绑定！该手机号共有 {len(new_ids)} 个 bot",
            "bot_ids": new_ids
        }
    finally:
        await conn.close()


# ── 共享路径查询 ──

@router.get("/shared-path")
async def shared_path(bot_id: str = Query(..., description="bot_id")):
    """获取 bot 所在手机号的共享文件目录"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT phone FROM user_accounts WHERE $1 = ANY(bot_ids)",
            bot_id
        )
        if not row:
            raise HTTPException(404, "该 bot 未绑定手机号，无法使用共享存储")

        phone = row["phone"]
        phone_dir = phone.replace("+", "").replace("-", "")
        path = os.path.join(SHARED_DIR, phone_dir)
        os.makedirs(path, exist_ok=True)

        return {
            "phone": phone,
            "shared_dir": path,
            "bot_id": bot_id,
            "note": "此目录下文件对所有绑定同手机号的 bot 可见"
        }
    finally:
        await conn.close()
