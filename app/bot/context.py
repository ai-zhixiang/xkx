"""微侠 WeClaw v2 — 上下文加载器
取对话历史 + 用户记忆 → 拼 system prompt + messages[]
"""
import json, logging
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("weclawd.context")

MAX_HISTORY = 50          # 最多加载的历史消息数
MAX_FACTS = 10             # 最多注入的事实数
PERSONA = """你是 🦞 享客虾，AI 创作伙伴。
你在中国深圳，由智享家团队开发。

## 能力
- 帮用户做嗨卡（AI贺卡）
- 推荐和搜索音乐
- 聊天陪伴
- 写歌创作引导

## 风格
- 朋友式聊天，不说「作为AI」「我是AI」
- 简洁直接，不用太多修饰
- 用户问什么答什么"""


async def load_session_messages(
    db: AsyncSession,
    session_id: str,
    limit: int = MAX_HISTORY,
) -> list[dict]:
    """加载对话历史"""
    rows = await db.execute(
        text(
            "SELECT role, content, tool_calls FROM bot_session_messages_v2 "
            "WHERE session_id = :sid ORDER BY created_at ASC LIMIT :lim"
        ),
        {"sid": session_id, "lim": limit},
    )
    messages = []
    for row in rows:
        msg = {"role": row[0], "content": row[1]}
        if row[2]:
            msg["tool_calls"] = row[2]
        messages.append(msg)
    return messages


async def load_user_memory(
    db: AsyncSession,
    user_id: str,
) -> Optional[dict]:
    """加载用户记忆"""
    row = await db.execute(
        text("SELECT preferences, summary, facts FROM user_memories_v2 WHERE user_id = :uid"),
        {"uid": user_id},
    )
    r = row.fetchone()
    if r:
        return {
            "preferences": r[0] or {},
            "summary": r[1] or "",
            "facts": r[2] or [],
        }
    return None


def build_system_prompt(
    user_nickname: str = "",
    user_memory: Optional[dict] = None,
    extra_context: str = "",
) -> str:
    """构建 system prompt"""
    parts = [PERSONA]

    # 时间
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts.append(f"\n当前时间: {now} (北京时间)")

    # 用户信息
    if user_nickname:
        parts.append(f"当前用户: {user_nickname}")

    # 用户记忆
    if user_memory:
        prefs = user_memory.get("preferences", {})
        if prefs:
            prefs_str = " | ".join(f"{k}={v}" for k, v in prefs.items())
            parts.append(f"用户偏好: {prefs_str}")

        facts = user_memory.get("facts", [])
        if facts:
            parts.append("关于用户: " + " | ".join(facts[:MAX_FACTS]))

        summary = user_memory.get("summary", "")
        if summary:
            parts.append(f"近期话题: {summary}")

    # 额外上下文（如 restart 标记）
    if extra_context:
        parts.append(f"\n[上下文] {extra_context}")

    return "\n".join(parts)


async def build_messages(
    db: AsyncSession,
    session_id: str,
    user_id: str,
    user_content: str,
    user_nickname: str = "",
    extra_context: str = "",
) -> list[dict]:
    """取历史 + 记忆 → 拼完整 messages[] 给 DeepSeek"""
    # 1. 加载历史
    history = await load_session_messages(db, session_id)

    # 2. 加载记忆
    memory = await load_user_memory(db, user_id)

    # 3. 构建 system prompt
    system_prompt = build_system_prompt(user_nickname, memory, extra_context)

    # 4. 拼 messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    return messages


async def save_messages(
    db: AsyncSession,
    session_id: str,
    messages: list[dict],
):
    """保存新消息到 DB"""
    for msg in messages:
        tool_calls = msg.get("tool_calls")
        await db.execute(
            text(
                "INSERT INTO bot_session_messages_v2 (session_id, role, content, tool_calls) "
                "VALUES (:sid, :role, :content, :tc)"
            ),
            {
                "sid": session_id,
                "role": msg["role"],
                "content": msg["content"],
                "tc": json.dumps(tool_calls) if tool_calls else None,
            },
        )
    await db.commit()


async def update_user_memory(
    db: AsyncSession,
    user_id: str,
    preferences: Optional[dict] = None,
    facts: Optional[list] = None,
    summary: Optional[str] = None,
):
    """更新用户记忆"""
    # 先查是否存在
    row = await db.execute(
        text("SELECT preferences, facts FROM user_memories_v2 WHERE user_id = :uid"),
        {"uid": user_id},
    )
    existing = row.fetchone()

    if existing:
        # 合并更新
        new_prefs = {**(existing[0] or {}), **(preferences or {})}
        new_facts = list(set((existing[1] or []) + (facts or [])))
        if summary:
            await db.execute(
                text(
                    "UPDATE user_memories_v2 SET preferences=:prefs, facts=:facts, summary=:summary, updated_at=NOW() "
                    "WHERE user_id=:uid"
                ),
                {"uid": user_id, "prefs": json.dumps(new_prefs), "facts": json.dumps(new_facts), "summary": summary},
            )
        else:
            await db.execute(
                text(
                    "UPDATE user_memories_v2 SET preferences=:prefs, facts=:facts, updated_at=NOW() "
                    "WHERE user_id=:uid"
                ),
                {"uid": user_id, "prefs": json.dumps(new_prefs), "facts": json.dumps(new_facts)},
            )
    else:
        await db.execute(
            text(
                "INSERT INTO user_memories_v2 (user_id, preferences, facts, summary) "
                "VALUES (:uid, :prefs, :facts, :summary)"
            ),
            {
                "uid": user_id,
                "prefs": json.dumps(preferences or {}),
                "facts": json.dumps(facts or []),
                "summary": summary or "",
            },
        )
    await db.commit()
