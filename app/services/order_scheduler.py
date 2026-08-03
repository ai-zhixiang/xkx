"""微侠令 — 调度引擎

职责：用户确认令后自动路由到对应节点执行，回写结果，推送到用户

调度流程：
  确认令 → status=executing
    ↓
  调度引擎轮询 → 路由节点 → 执行 → 回写结果 → 推送通知
"""
import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from typing import Optional
from uuid import UUID

import aiohttp

# ── 配置 ──

ORDER_API = "http://127.0.0.1:8001/api/orders"
KEEPALIVE_API = "http://127.0.0.1:9100/api/notify-order"
HERMES_API = "http://127.0.0.1:8642"
HERMES_API_KEY = "sk-xiakexia-external-server"
POLL_INTERVAL = 3  # 秒

# SSH 节点配置
SSH_CONFIG = {
    "bear2": {"host": "124.222.215.111", "user": "ubuntu"},
    "hk":   {"host": "124.156.173.120", "user": "ubuntu"},
    "md1":  {"host": "81.71.99.46", "user": "ubuntu"},
    "main": {"host": "162.14.111.56", "user": "ubuntu"},
}

# 节点路由：关键词 → 执行节点
NODE_ROUTER = [
    (["文档", "文件", "备份", "归档", "同步", "磁盘", "md1", "md-1", "df ", "du "], "md1"),
    (["部署", "代码", "git", "运维", "重启", "安装", "配置", "编译", "测试", "bear2", "熊二"], "bear2"),
    (["量化", "交易", "搬砖", "K线", "策略", "行情", "买卖", "hk", "香港"], "hk"),
    (["提醒", "闹钟", "相册", "照片", "电话", "短信", "闹铃"], "local"),
    (["搜索", "分析", "查询", "总结", "写", "翻译", "解释", "推荐", "比较", "评价", "查看", "告诉", "列出", "介绍", "说明", "生成", "画", "设计", "规划"], "ai"),
]

log = logging.getLogger("order_scheduler")


# ── 调度核心 ──

async def start_scheduler():
    """启动调度引擎（后台任务）"""
    log.info("📋 调度引擎启动，轮询间隔 %ds", POLL_INTERVAL)
    while True:
        try:
            await poll_and_dispatch()
        except Exception as e:
            log.error("调度轮询异常: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


async def poll_and_dispatch():
    """轮询待调度的令（status=executing, execute_node=null）"""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(
            f"{ORDER_API}/all",
            params={"status": "executing", "size": 50}
        ) as r:
            if r.status != 200:
                return
            data = await r.json()
            items = data.get("data", {}).get("items", [])

        for item in items:
            if item.get("execute_node"):
                continue

            order_id = item["id"]
            title = item.get("title", "")
            content = item.get("content", "") or ""
            user_id = item.get("user_id", "")
            source_channel = item.get("source_channel", "weixin")

            log.info("📋 调度令: %s → %s", order_id[:8], title[:30])

            node = route_node(title + " " + content)
            log.info("   → 节点: %s", node)

            await update_execute_node(s, order_id, node)

            try:
                result = await execute(s, order_id, title, content, node)
                log.info("   ✅ 完成: %s", result.get("summary", "")[:50])

                # ── 执行完成，推送通知 ──
                await notify_user(user_id, title, result.get("summary", ""), "completed", source_channel)
            except Exception as e:
                log.error("   ❌ 执行失败: %s", e)
                await complete_order(s, order_id, error_message=str(e))
                await notify_user(user_id, title, str(e)[:200], "failed", source_channel)


def route_node(text: str) -> str:
    """关键词匹配 → 确定执行节点"""
    text_lower = text.lower()
    shell_indicators = ["&&", "||", "| tail", "| head", "| grep", "ls ", "ps ", "cat ",
                        "sudo ", "apt ", "pip ", "npm ", "docker", "systemctl"]
    for ind in shell_indicators:
        if ind in text_lower:
            return "md1"
    for keywords, node in NODE_ROUTER:
        for kw in keywords:
            if kw in text_lower:
                return node
    return "ai"


async def update_execute_node(session, order_id, node):
    await session.patch(f"{ORDER_API}/{order_id}", json={"execute_node": node})
    await session.patch(f"{ORDER_API}/{order_id}/progress", json={"progress": 10})


# ── 推送通知 ──

async def notify_user(user_id: str, title: str, summary: str, status: str, source_channel: str):
    """执行完成/失败后，主动推送给用户"""
    # 把 user_id 转成 iLink 格式
    ilink_user = user_id
    if not ilink_user.endswith("@im.wechat") and source_channel in ("weixin", "wx", "微信"):
        ilink_user = f"{ilink_user}@im.wechat"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.post(KEEPALIVE_API, json={
                "to_user": ilink_user,
                "title": title,
                "summary": summary[:300],
                "status": status,
                "source_channel": source_channel,
            })
            log.info("   📨 推送通知: %s", status)
    except Exception as e:
        log.warning("   📨 推送失败: %s", e)


async def execute(session, order_id, title, content, node, source_channel="weixin"):
    """执行令，返回执行结果"""
    if node == "ai":
        return await execute_ai(order_id, title, content, source_channel)
    elif node in SSH_CONFIG:
        return await execute_ssh(order_id, node, title, content, source_channel)
    elif node == "local":
        return await execute_local(order_id, title, content, source_channel)
    else:
        raise ValueError(f"未知节点: {node}")


# ── AI 执行器 ──

async def execute_ai(order_id, title, content, source_channel="weixin"):
    prompt = f"{title}\n{content}" if content else title
    result_text = ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek/deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                }
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    result_text = data["choices"][0]["message"]["content"]
                else:
                    error_text = await r.text()
                    result_text = f"(API 错误: {r.status})"
                    log.error("OpenRouter API 错误: %s", error_text[:200])
    except Exception as e:
        result_text = f"(API 调用异常: {str(e)[:100]})"
        log.error("DeepSeek API 异常: %s", e)

    result_text = result_text.strip() or "(无返回结果)"
    await complete_order_http(order_id, result={"summary": result_text[:500], "full": result_text})
    return {"summary": result_text[:200], "node": "ai"}


# ── SSH 执行器 ──

async def execute_ssh(order_id, node, title, content, source_channel="weixin"):
    config = SSH_CONFIG[node]
    cmd = content or title
    ssh_cmd = [
        "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
        f"{config['user']}@{config['host']}",
        cmd
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode("utf-8", errors="replace").strip()
        error = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            await complete_order_http(order_id, result={
                "summary": f"✅ {node}: 执行成功",
                "output": output[:2000],
                "node": node,
                "exit_code": proc.returncode,
            })
            return {"summary": output[:200], "node": node}
        else:
            raise RuntimeError(f"SSH 执行失败 (exit={proc.returncode}): {error[:200]}")
    except asyncio.TimeoutError:
        raise RuntimeError(f"SSH 执行超时 (60s)")


async def execute_local(order_id, title, content, source_channel="weixin"):
    await complete_order_http(order_id, result={
        "summary": "📱 已推送任务到手机端",
        "note": "本地执行需微侠 APP 支持",
        "node": "local",
    })
    return {"summary": "已推送", "node": "local"}


# ── 通用函数 ──

async def complete_order_http(order_id, result=None, error_message=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        await s.patch(
            f"{ORDER_API}/{order_id}/complete",
            json={"result": result or {}, "error_message": error_message}
        )


async def complete_order(session, order_id, result=None, error_message=None):
    await session.patch(
        f"{ORDER_API}/{order_id}/complete",
        json={"result": result or {}, "error_message": error_message}
    )
