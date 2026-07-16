"""微侠令 — Keepalive 指令匹配层

插在 keepalive 消息处理管线：暗号/项目口令/项目命令 → 令指令 → @weclaw → 会员 → Agent

唤醒词：! 或 ！ — 只有以 !/！ 开头的消息才会被处理为令指令
"""
import re
import logging
import aiohttp

ORDER_API = "http://127.0.0.1:8001/api/orders"
log = logging.getLogger("keepalive.orders")

ORDER_PATTERNS = [
    (re.compile(r'^令$'), 'list_pending'),
    (re.compile(r'^令\s+(.+)$'), 'create_order'),
    (re.compile(r'^查令$'), 'list_pending'),
    (re.compile(r'^我的令$'), 'list_pending'),
    (re.compile(r'^我的待办$'), 'list_pending'),
    (re.compile(r'^确认令[\s：:]*(.+)$'), 'confirm_order'),
    (re.compile(r'^确认所有$'), 'confirm_all'),
    (re.compile(r'^驳回令[\s：:]*(.+)$'), 'reject_order'),
    (re.compile(r'^取消令[\s：:]*(.+)$'), 'cancel_order'),
    (re.compile(r'^删除令[\s：:]*(.+)$'), 'delete_order'),
    (re.compile(r'^进度[\s：:]*(.+)$'), 'update_progress'),
]


def _strip_wake(text: str) -> str | None:
    raw = text.strip()
    if raw.startswith('!') or raw.startswith('！'):
        return raw[1:].strip()
    return None


async def match_order_command(text: str, user_id: str, channel: str) -> str | None:
    cmd = _strip_wake(text)
    if cmd is None:
        return None

    uid = user_id.split("@")[0]

    for pattern, action in ORDER_PATTERNS:
        m = pattern.match(cmd)
        if not m:
            continue

        if action == 'list_pending':
            return await _list_pending(uid)
        elif action == 'create_order':
            return await _create_order(m.group(1).strip(), uid, channel)
        elif action == 'confirm_order':
            return await _confirm_order(m.group(1).strip(), channel, uid)
        elif action == 'confirm_all':
            return await _confirm_all(uid, channel)
        elif action == 'reject_order':
            return await _reject_order(m.group(1).strip(), channel, uid)
        elif action == 'cancel_order':
            return await _cancel_order(m.group(1).strip(), channel, uid)
        elif action == 'delete_order':
            return await _delete_order(m.group(1).strip(), uid)
        elif action == 'update_progress':
            return await _update_progress(m.group(1).strip(), channel, uid)

    return (
        "📋 令指令一览：\n"
        "  !令               — 查看待确认的令\n"
        "  !令 <描述>        — 创建新令\n"
        "  !确认令 <编号>    — 确认执行\n"
        "  !确认所有         — 确认全部\n"
        "  !驳回令 <编号>    — 拒绝\n"
        "  !取消令 <编号>    — 取消"
    )


# ── 数据层 ──

async def _fetch_pending(user_id: str) -> list:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            params = {"user_id": user_id, "status": "pending", "size": 50}
            async with s.get(ORDER_API, params=params) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                return data.get("data", {}).get("items", [])
    except Exception:
        return []


async def _resolve_by_number(num_str: str, user_id: str) -> tuple[str | None, str | None]:
    """编号 → (order_id, title)"""
    try:
        num = int(num_str)
    except ValueError:
        return None, None
    orders = await _fetch_pending(user_id)
    if num < 1 or num > len(orders):
        return None, None
    o = orders[num - 1]
    return o["id"], o["title"]


async def _get_order_number(order_id: str, user_id: str) -> int:
    orders = await _fetch_pending(user_id)
    for i, o in enumerate(orders, 1):
        if o["id"] == order_id:
            return i
    return 1


async def _patch_order(order_id: str, action: str, channel: str) -> str | None:
    """通用 PATCH 调用，返回 success 返回 error 文本"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.patch(f"{ORDER_API}/{order_id}/{action}", json={"channel": channel}) as r:
                if r.status == 200:
                    data = await r.json()
                    return None, data["data"]["title"]
                err = await r.json()
                return f"❌ {err.get('detail', f'失败({r.status})')}", None
    except Exception as e:
        log.error(f"{action}令异常: %s", e)
        return "❌ 操作失败（服务不可达）", None


# ── 操作 ──

async def _create_order(description: str, user_id: str, channel: str) -> str:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            payload = {
                "user_id": user_id,
                "title": description,
                "content": description,
                "source_channel": channel,
            }
            async with s.post(ORDER_API, json=payload) as r:
                if r.status != 200:
                    return f"❌ 创建失败（{r.status}）"
                data = await r.json()
                order_id = data["data"]["id"]
            async with s.post(f"{ORDER_API}/{order_id}/submit") as r2:
                if r2.status != 200:
                    return f"⚠️ 已创建但提交失败"
            return f"✅ 已创建令「{description}」\n💬 !确认令 {await _get_order_number(order_id, user_id)} 执行"
    except Exception as e:
        log.error("创建令异常: %s", e)
        return "❌ 创建失败（服务不可达）"


async def _list_pending(user_id: str) -> str:
    orders = await _fetch_pending(user_id)
    if not orders:
        return "📋 暂无待确认的令"
    lines = ["📋 待确认的令："]
    for i, item in enumerate(orders, 1):
        lines.append(f"  {i}. {item['title']}")
        lines.append(f"     · {item['created_at'][:16]}")
    lines.append("")
    lines.append("💬 !确认令 <编号> · !驳回令 <编号> · !取消令 <编号> · !确认所有")
    return "\n".join(lines)


async def _confirm_order(arg: str, channel: str, user_id: str) -> str:
    oid, title = await _resolve_by_number(arg, user_id)
    if not oid:
        return f"❌ 未找到令「{arg}」"
    err, _ = await _patch_order(oid, "confirm", channel)
    return f"✅ 已确认执行：{title}" if err is None else err


async def _confirm_all(user_id: str, channel: str) -> str:
    orders = await _fetch_pending(user_id)
    if not orders:
        return "📋 暂无待确认的令"
    success = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            for item in orders:
                async with s.patch(f"{ORDER_API}/{item['id']}/confirm", json={"channel": channel}) as cr:
                    if cr.status == 200:
                        success += 1
        msg = f"✅ 已确认 {success}/{len(orders)} 个令"
        if success < len(orders):
            msg += f"，{len(orders) - success} 个失败"
        return msg
    except Exception as e:
        log.error("确认全部异常: %s", e)
        return "❌ 操作失败（服务不可达）"


async def _reject_order(arg: str, channel: str, user_id: str) -> str:
    oid, title = await _resolve_by_number(arg, user_id)
    if not oid:
        return f"❌ 未找到令「{arg}」"
    err, _ = await _patch_order(oid, "reject", channel)
    return f"⛔ 已驳回：{title}" if err is None else err


async def _cancel_order(arg: str, channel: str, user_id: str) -> str:
    oid, title = await _resolve_by_number(arg, user_id)
    if not oid:
        return f"❌ 未找到令「{arg}」"
    err, _ = await _patch_order(oid, "cancel", channel)
    return f"🚫 已取消：{title}" if err is None else err


async def _delete_order(arg: str, user_id: str) -> str:
    oid, title = await _resolve_by_number(arg, user_id)
    if not oid:
        return f"❌ 未找到令「{arg}」"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.delete(f"{ORDER_API}/{oid}") as r:
                if r.status == 204:
                    return f"🗑️ 已删除：{title}"
                return f"❌ 删除失败（{r.status}）"
    except Exception as e:
        log.error("删除令异常: %s", e)
        return "❌ 删除失败（服务不可达）"


async def _update_progress(arg: str, channel: str, user_id: str) -> str:
    return "ℹ️ !进度 <编号> 完成了<百分比>"
