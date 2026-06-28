#!/usr/bin/env python3
"""
Keepalive Service — 保活层
职责: iLink长轮询 + 暗号秒回 + 欢迎推送 + session保活
不调Hermes，不处理AI逻辑，不依赖网关。

非暗号消息 → POST 到 Agent Connector (:9101) → 等回复 → 推回iLink
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import aiohttp

# ══════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════

# iLink
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0  # 131584

LONG_POLL_TIMEOUT_MS = 35_000
SHORT_POLL_TIMEOUT_MS = 5_000
API_TIMEOUT_MS = 15_000
SESSION_EXPIRED_ERRCODE = -14
SESSION_PAUSE_MS = 10 * 60 * 1000  # 10min
HEARTBEAT_INTERVAL_MS = 5 * 60 * 1000  # 5min

# Agent Connector
AGENT_URL = "http://127.0.0.1:9101/api/message"
AGENT_TIMEOUT = 120  # Hermes 最长回复时间

# 免费用户限额
FREE_DAILY_LIMIT = 50

# HTTP API (本服务)
HTTP_PORT = 9100

# DB
DB_DSN = "postgresql://lucky:lucky_pass@localhost:5432/weclawd"
DB_RELOAD_INTERVAL = 30  # 30s

# Paths
STATE_DIR = Path.home() / ".hermes" / "keepalive"
SYNC_BUF_DIR = STATE_DIR / "sync_buf"
PAUSE_DIR = STATE_DIR / "pause"
CONFIG_DIR = Path.home() / "weclaw-1" / "config"
CODE_PHRASE_FILE = CONFIG_DIR / "access_codes.json"

STATE_DIR.mkdir(parents=True, exist_ok=True)
SYNC_BUF_DIR.mkdir(parents=True, exist_ok=True)
PAUSE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Message types
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VIDEO = 3
ITEM_FILE = 4
ITEM_VOICE = 5

TYPING_START = 1
TYPING_STOP = 2

# ══════════════════════════════════════════════
# 暗号管理
# ══════════════════════════════════════════════

_DEFAULT_CODES = {
    "codes": [
        {"code": "天王盖地虎", "reply": "OpenClaw 是SB！", "level": "admin"},
        {"code": "宝塔镇河妖", "reply": "微侠真牛逼！", "level": "admin"},
        {"code": "微侠真牛逼", "reply": "天王盖地虎！同志！", "level": "admin"},
        {"code": "openclaw是sb", "reply": "宝塔镇河妖！收到！", "level": "admin"},
    ]
}

def _ensure_code_file():
    if not CODE_PHRASE_FILE.exists():
        CODE_PHRASE_FILE.write_text(json.dumps(_DEFAULT_CODES, ensure_ascii=False, indent=2))

def load_code_phrases() -> list[dict]:
    _ensure_code_file()
    try:
        data = json.loads(CODE_PHRASE_FILE.read_text())
        return data.get("codes", [])
    except (json.JSONDecodeError, FileNotFoundError):
        return _DEFAULT_CODES["codes"]

def match_code_phrase(text: str) -> Optional[dict]:
    stripped = re.sub(r'[，。！？、；：""\'\s]', '', text).lower()
    codes = load_code_phrases()
    for entry in codes:
        code = re.sub(r'[，。！？、；：""\'\s]', '', entry["code"]).lower()
        if code in stripped:
            return entry
    return None

# ══════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════

def _build_base_info() -> dict:
    return {"channel_version": "2.2.0"}

def _build_headers(token: str, body: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if body:
        headers["Content-Length"] = str(len(body.encode("utf-8")))
        uin_val = struct.unpack(">I", secrets.token_bytes(4))[0]
        headers["X-WECHAT-UIN"] = base64.b64encode(str(uin_val).encode("utf-8")).decode("ascii")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)

def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES128(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()

def _aes_padded_size(size: int) -> int:
    block = 16
    return ((size + block - 1) // block) * block

def _safe_key(s: str) -> str:
    return s.replace("@", "_at_").replace(".", "_dot_")

def _sync_path(bot_id: str) -> Path:
    return SYNC_BUF_DIR / f"{_safe_key(bot_id)}.json"

def _pause_path(bot_id: str) -> Path:
    return PAUSE_DIR / f"{_safe_key(bot_id)}.json"

def _make_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log

root_log = _make_logger("keepalive")

# Session persistence
def load_sync_buf(bot_id: str) -> str:
    try:
        return json.loads(_sync_path(bot_id).read_text()).get("buf", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

def save_sync_buf(bot_id: str, buf: str):
    _sync_path(bot_id).write_text(json.dumps({"buf": buf}))

def is_paused(bot_id: str) -> bool:
    try:
        until = json.loads(_pause_path(bot_id).read_text()).get("until", 0)
        if time.time() * 1000 < until:
            return True
        _pause_path(bot_id).unlink(missing_ok=True)
        return False
    except (FileNotFoundError, json.JSONDecodeError):
        return False

def pause_bot(bot_id: str, duration_ms: int = SESSION_PAUSE_MS):
    until = int(time.time() * 1000 + duration_ms)
    _pause_path(bot_id).write_text(json.dumps({"until": until}))

def clear_pause(bot_id: str):
    _pause_path(bot_id).unlink(missing_ok=True)

def pause_remaining_ms(bot_id: str) -> int:
    try:
        until = json.loads(_pause_path(bot_id).read_text()).get("until", 0)
        return max(0, until - int(time.time() * 1000))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0

# ══════════════════════════════════════════════
# iLink API
# ══════════════════════════════════════════════

async def _ilink_post(endpoint: str, payload: dict, token: str,
                      timeout_ms: int = API_TIMEOUT_MS,
                      session: Optional[aiohttp.ClientSession] = None) -> dict:
    body = json.dumps({**payload, "base_info": _build_base_info()}, separators=(",", ":"))
    headers = _build_headers(token, body)

    async def _do(s: aiohttp.ClientSession) -> dict:
        async with s.post(f"{ILINK_BASE_URL}/{endpoint}",
                          data=body, headers=headers,
                          timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000)) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {await r.text()}")
            raw = await r.read()
            return json.loads(raw)

    if session:
        return await _do(session)
    async with aiohttp.ClientSession() as s:
        return await _do(s)

async def notify_start(token: str) -> bool:
    try:
        await _ilink_post("ilink/bot/msg/notifystart", {}, token, timeout_ms=10_000)
        return True
    except Exception:
        return False

async def notify_stop(token: str) -> bool:
    try:
        await _ilink_post("ilink/bot/msg/notifystop", {}, token, timeout_ms=5_000)
        return True
    except Exception:
        return False

async def get_updates(token: str, sync_buf: str, timeout_ms: int,
                      session: aiohttp.ClientSession) -> dict:
    body = json.dumps({
        "get_updates_buf": sync_buf,
        "base_info": _build_base_info(),
    }, separators=(",", ":"))
    headers = _build_headers(token, body)
    try:
        async with session.post(f"{ILINK_BASE_URL}/ilink/bot/getupdates",
                                data=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000)) as r:
            if r.status != 200:
                return {"ret": -1, "errcode": r.status}
            raw = await r.read()
            return json.loads(raw)
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}

async def _ilink_get_config(token: str, user_id: str,
                            context_token: Optional[str] = None,
                            session: Optional[aiohttp.ClientSession] = None) -> dict:
    payload = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _ilink_post("ilink/bot/getconfig", payload, token,
                             timeout_ms=10_000, session=session)

# ══════════════════════════════════════════════
# 消息发送
# ══════════════════════════════════════════════

async def send_text(token: str, to_user_id: str, text: str,
                    context_token: str = "",
                    session: Optional[aiohttp.ClientSession] = None):
    msg = {
        "from_user_id": "", "to_user_id": to_user_id,
        "client_id": str(int(time.time() * 1000)),
        "message_type": MSG_TYPE_BOT, "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    try:
        resp = await _ilink_post("ilink/bot/sendmessage", {"msg": msg}, token,
                                 timeout_ms=10_000, session=session)
        root_log.info("[send] ✓ ret=%s", resp.get("ret"))
    except Exception as e:
        root_log.warning("[send] 失败: %s", e)

async def send_typing(token: str, to_user_id: str, typing_ticket: str,
                      status: int, session: Optional[aiohttp.ClientSession] = None):
    try:
        await _ilink_post("ilink/bot/sendtyping", {
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        }, token, timeout_ms=10_000, session=session)
    except Exception:
        pass

# ══════════════════════════════════════════════
# DB
# ══════════════════════════════════════════════

async def load_bots() -> list[tuple[str, str]]:
    try:
        import asyncpg
        conn = await asyncpg.connect(DB_DSN)
        try:
            rows = await conn.fetch(
                "SELECT bot_id, bot_token FROM bot_accounts "
                "WHERE is_active = true AND bot_token IS NOT NULL AND bot_token != ''"
            )
            return [(r["bot_id"], r["bot_token"]) for r in rows]
        finally:
            await conn.close()
    except Exception as e:
        root_log.error("DB 查询失败: %s", e)
        return []

# ══════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════

async def _wait(seconds: float, shutdown: asyncio.Event):
    try:
        await asyncio.wait_for(asyncio.get_event_loop().create_future(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        raise

# ══════════════════════════════════════════════
# Bot Session
# ══════════════════════════════════════════════

@dataclass
class BotSession:
    bot_id: str
    token: str
    log: logging.Logger
    sync_buf: str = ""
    last_activity: float = 0.0
    consecutive_failures: int = 0
    user_typing_tickets: dict = None  # user_id → typing_ticket
    welcome_sent: set = None  # set of user_ids that got welcome
    pending_quota_reminder: dict = None  # user_id → "剩余X次"

    def __post_init__(self):
        self.user_typing_tickets = self.user_typing_tickets or {}
        self.welcome_sent = self.welcome_sent or set()
        self.pending_quota_reminder = self.pending_quota_reminder or {}

# ══════════════════════════════════════════════
# Agent Connector HTTP 调用
# ══════════════════════════════════════════════

async def forward_to_agent(bot_id: str, from_user: str, text: str,
                           msg_id: str, context_token: str) -> Optional[str]:
    """POST 消息到 Agent Connector，等回复"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AGENT_TIMEOUT)) as s:
            async with s.post(AGENT_URL, json={
                "bot_id": bot_id,
                "from_user": from_user,
                "text": text,
                "msg_id": msg_id,
                "context_token": context_token,
            }) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("reply")
    except asyncio.TimeoutError:
        root_log.warning("[agent] 超时(120s)")
    except Exception as e:
        root_log.warning("[agent] 失败: %s", e)
    return None

# ══════════════════════════════════════════════
# 主轮询循环
# ══════════════════════════════════════════════

async def run_bot(bot_id: str, token: str, shutdown: asyncio.Event):
    log = _make_logger(f"bot:{bot_id[:14]}")
    bot = BotSession(bot_id=bot_id, token=token, log=log)
    register_bot(bot_id, bot)
    log.info("🤖 启动")

    # 预热
    has_old = bool(load_sync_buf(bot_id))
    if has_old:
        await notify_stop(token)
        await asyncio.sleep(2)
    else:
        log.info("  新 Bot，跳过 notifyStop")

    try:
        # ... session 循环 ...
        async with aiohttp.ClientSession() as session:
            while not shutdown.is_set():
                try:
                    await _run_session(bot, session, shutdown)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error("❌ session 崩溃: %s", e)
                    bot.consecutive_failures += 1
                    if bot.consecutive_failures >= 3:
                        log.error("连续 3 次崩溃，暂停 10min")
                        await _wait(600, shutdown)
                        bot.consecutive_failures = 0

        await notify_stop(token)
    finally:
        unregister_bot(bot_id)
    log.info("🤖 已停止")

async def _run_session(bot: BotSession, session: aiohttp.ClientSession,
                       shutdown: asyncio.Event):
    log = bot.log
    bot.last_activity = time.time()

    # 恢复 sync_buf
    bot.sync_buf = load_sync_buf(bot.bot_id)
    if bot.sync_buf:
        log.info("  恢复 sync_buf (%d bytes)", len(bot.sync_buf))

    # notifyStart
    if not await notify_start(bot.token):
        log.warning("  notifyStart 失败，重试...")
        await _wait(5, shutdown)
        if not await notify_start(bot.token):
            log.error("  notifyStart 连续失败，跳过")
            return
    log.info("✅ notifyStart ✓")

    next_timeout = LONG_POLL_TIMEOUT_MS

    while not shutdown.is_set():
        # 暂停检查
        if is_paused(bot.bot_id):
            remaining = pause_remaining_ms(bot.bot_id)
            log.info("⏳ 暂停中，剩余 %dmin", remaining // 60_000)
            await _wait(min(remaining / 1000, 600), shutdown)
            clear_pause(bot.bot_id)
            bot.consecutive_failures = 0
            await notify_stop(bot.token)
            await asyncio.sleep(2)
            return  # 外层重建 session

        try:
            resp = await get_updates(bot.token, bot.sync_buf, next_timeout, session)
            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            is_error = (ret != 0) or (errcode != 0)

            if is_error:
                if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                    log.error("🚫 session 过期，暂停 10min")
                    pause_bot(bot.bot_id)
                    await _wait(10, shutdown)
                    bot.consecutive_failures = 0
                    return
                bot.consecutive_failures += 1
                log.warning("getUpdates 错误 ret=%s err=%s (%d/3)", ret, errcode, bot.consecutive_failures)
                if bot.consecutive_failures >= 3:
                    await _wait(30, shutdown)
                    bot.consecutive_failures = 0
                else:
                    await _wait(2, shutdown)
                continue

            # 成功
            bot.consecutive_failures = 0
            bot.last_activity = time.time()

            if resp.get("longpolling_timeout_ms"):
                next_timeout = resp["longpolling_timeout_ms"]

            # sync_buf 持久化
            new_buf = resp.get("get_updates_buf", "")
            if new_buf and new_buf != bot.sync_buf:
                save_sync_buf(bot.bot_id, new_buf)
                bot.sync_buf = new_buf

            # 处理消息
            for msg in resp.get("msgs", []):
                from_user = msg.get("from_user_id", "")
                if not from_user:
                    continue

                ctx = msg.get("context_token", "")
                text = _extract_text(msg)

                if not text:
                    continue

                log.info("📩 %s: %s", from_user[:20], text[:60])

                # ── 1. 暗号匹配（秒回，不经过 Agent）──
                code_match = match_code_phrase(text)
                if code_match:
                    reply = code_match["reply"]
                    log.info("🔐 暗号: %s → %s", text[:12], reply)
                    await send_text(bot.token, from_user, reply, ctx, session)
                    continue

                # ── 1.4 会员指令（查询，不经过 Agent）──
                _cmd_text = text.strip().lower()
                if _cmd_text in ("会员", "我的套餐", "套餐", "查会员", "查套餐", "vip", "我的会员"):
                    uid = from_user.split("@")[0]
                    try:
                        import asyncpg
                        _qdb = await asyncpg.connect(DB_DSN)
                        try:
                            row = await _qdb.fetchrow(
                                "SELECT s.status, s.expires_at, p.name as plan_name, s.started_at "
                                "FROM subscribers s LEFT JOIN plans p ON s.plan_id = p.id "
                                "WHERE s.openid = $1", uid
                            )
                            nick = ""
                            try:
                                _nq_nick = await asyncpg.connect(DB_DSN)
                                try:
                                    _nr_nick = await _nq_nick.fetchrow("SELECT nickname FROM channel_bindings WHERE channel_user_id LIKE $1 LIMIT 1", uid + "%")
                                    if _nr_nick and _nr_nick["nickname"]:
                                        nick = _nr_nick["nickname"]
                                finally:
                                    await _nq_nick.close()
                            except:
                                pass
                            if row and str(row["status"]).upper() in ("ACTIVE", "TRIAL") and row["expires_at"] and row["expires_at"] >= date.today():
                                remain_days = (row["expires_at"] - date.today()).days

                                reply_text = (
                                    f"🦞 享客虾会员 · {row['plan_name'] or '已开通'}\n\n"
                                    f"📅 到期：{row['expires_at']}（剩余 {remain_days} 天）\n"
                                    f"📆 开通：{row['started_at']}\n\n"
                                    f"👉 https://ai.pangoozn.com/go/s/" + uid + " ——续费"
                                )
                            else:
                                reply_text = (
                                    f"🦞 你当前是免费用户\n\n"
                                    f"每日 {FREE_DAILY_LIMIT} 条免费对话\n"
                                    f"开通会员可享不限量对话 + AI写歌等全部功能\n\n"
                                    f"👉 https://ai.pangoozn.com/go/s/" + uid + " ——点此开通"
                                )
                        finally:
                            await _qdb.close()
                    except Exception as _qe:
                        root_log.warning("会员查询异常: %s", _qe)
                        reply_text = "⚠️ 查询失败，请稍后再试"
                    
                    await send_text(bot.token, from_user, reply_text, ctx, session)
                    continue

                if _cmd_text in ("开通", "订阅", "购买"):
                    uid = from_user.split("@")[0]
                    nick = ""
                    try:
                        import asyncpg
                        _q2 = await asyncpg.connect(DB_DSN)
                        try:
                            r2 = await _q2.fetchrow("SELECT nickname FROM channel_bindings WHERE channel_user_id LIKE $1 LIMIT 1", uid + "%")
                            if r2 and r2["nickname"]:
                                nick = r2["nickname"]
                        finally:
                            await _q2.close()
                    except Exception:
                        pass
                    import urllib.parse
                    link = "https://ai.pangoozn.com/subscribe?openid=" + uid + "&nickname=" + urllib.parse.quote(nick or "虾友")
                    reply_text = (
                        "🦞 开通享客虾会员\n\n"
                        "🔥 公测特惠 · 限时一折\n"
                        "• 基础月卡 ¥9.9/月（原价¥99）\n"
                        "• 基础年卡 ¥99/年（原价¥990）\n\n"
                        "👇 点此开通\n"
                        + link + "\n\n"
                        "回复「会员」查看当前状态"
                    )
                    await send_text(bot.token, from_user, reply_text, ctx, session)
                    continue

                # ── 1.5 欢迎消息（新用户首次消息，附在Agent回复前）──
                if from_user not in bot.welcome_sent:
                    nickname = ""
                    # 查 DB 取昵称
                    try:
                        uid = from_user.split("@")[0]
                        import asyncpg
                        ndb = await asyncpg.connect(DB_DSN)
                        try:
                            row = await ndb.fetchrow(
                                "SELECT nickname FROM channel_bindings WHERE channel_user_id LIKE $1 LIMIT 1",
                                uid + "%"
                            )
                            if row and row["nickname"]:
                                nickname = row["nickname"]
                        finally:
                            await ndb.close()
                    except Exception:
                        pass
                    # fallback: iLink 给的昵称
                    if not nickname:
                        nickname = msg.get("from_nickname", "") or msg.get("from_user_name", "")
                    # 最终 fallback
                    if not nickname:
                        nickname = "虾友"
                    welcome = (
                        f"✨ 欢迎你，{nickname}！\n\n"
                        f"🦞 享客虾 Bot 已就绪 ✅\n\n"
                        f"有需要随时招呼。"
                    )
                    # 先发欢迎，再继续转发Agent
                    asyncio.create_task(send_text(bot.token, from_user, welcome, ctx, session))
                    bot.welcome_sent.add(from_user)

                # ── 1.6 会员/配额检查 ──
                uid = from_user.split("@")[0]
                _q_member = False
                try:
                    import asyncpg
                    _q_db = await asyncpg.connect(DB_DSN)
                    try:
                        row = await _q_db.fetchrow(
                            "SELECT status, expires_at FROM subscribers WHERE openid = $1",
                            uid
                        )
                        if row:
                            st = str(row["status"]).upper()
                            exp = row["expires_at"]
                            _q_member = st in ("ACTIVE", "TRIAL") and exp and exp >= date.today()

                        if not _q_member:
                            today_d = date.today()
                            qrow = await _q_db.fetchrow(
                                "SELECT used FROM daily_quota WHERE user_id = $1 AND quota_date = $2",
                                uid, today_d
                            )
                            used = qrow["used"] if qrow else 0

                            if used >= FREE_DAILY_LIMIT:
                                await send_text(bot.token, from_user,
                                    f"🦞 今日免费 {FREE_DAILY_LIMIT} 条对话已用完~\n\n"
                                    f"开通享客虾会员 ¥9.9/月起，畅聊无限 ✨\n"
                                    f"https://ai.pangoozn.com/subscribe",
                                    ctx, session)
                                await _q_db.close()
                                continue

                            # 计费
                            await _q_db.execute(
                                "INSERT INTO daily_quota (user_id, quota_date, used) VALUES ($1, $2, 1) "
                                "ON CONFLICT (user_id, quota_date) DO UPDATE SET used = daily_quota.used + 1",
                                uid, today_d
                            )

                            remaining = FREE_DAILY_LIMIT - used - 1
                            if remaining <= 3 and remaining > 0:
                                bot.pending_quota_reminder[from_user] = f"（剩余 {remaining} 次免费对话）"
                            elif used == 0:
                                bot.pending_quota_reminder[from_user] = f"今日还有 {FREE_DAILY_LIMIT} 次免费对话，开通会员不限量 👉 https://ai.pangoozn.com/subscribe"
                    finally:
                        await _q_db.close()
                except Exception as _qe:
                    log.warning("配额检查异常(放行): %s", _qe)

                # ── 2. 获取 typing ticket ──
                if from_user not in bot.user_typing_tickets:
                    try:
                        cfg = await _ilink_get_config(bot.token, from_user, ctx or None, session)
                        ticket = str(cfg.get("typing_ticket") or "")
                        if ticket:
                            bot.user_typing_tickets[from_user] = ticket
                    except Exception:
                        pass

                # ── 3. 发 typing ──
                ticket = bot.user_typing_tickets.get(from_user)
                if ticket:
                    asyncio.create_task(send_typing(bot.token, from_user, ticket, TYPING_START, session))

                # ── 4. 转发 Agent Connector ──
                reply = await forward_to_agent(bot.bot_id, from_user, text,
                                                msg.get("msg_id", ""), ctx)

                # ── 5. 停止 typing ──
                if ticket:
                    asyncio.create_task(send_typing(bot.token, from_user, ticket, TYPING_STOP, session))

                # ── 6. 发回复 ──
                if reply:
                    # 附上配额提示
                    qr = bot.pending_quota_reminder.pop(from_user, None)
                    if qr:
                        reply = reply + "\n\n" + qr
                    log.info("📤 %s", reply[:40])
                    await send_text(bot.token, from_user, reply, ctx, session)
                else:
                    log.warning("⚠️ Agent 无回复")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            bot.consecutive_failures += 1
            log.error("轮询异常 (%d/3): %s", bot.consecutive_failures, e)
            if bot.consecutive_failures >= 3:
                await _wait(30, shutdown)
                bot.consecutive_failures = 0
            else:
                await _wait(2, shutdown)

def _extract_text(msg: dict) -> str:
    for item in msg.get("item_list", []):
        if item.get("type") == 1:
            return (item.get("text_item") or {}).get("text", "")
        if item.get("type") == 4:
            voice = item.get("voice_item") or {}
            for key in ("asr_refer_text", "recog_text", "recognition", "text"):
                t = voice.get(key, "") or ""
                if t.strip():
                    return "[语音] " + t.strip()
            return "[语音] (识别失败)"
    return ""

# ══════════════════════════════════════════════
# HTTP API — 供 Agent Connector 调用来发消息
# ══════════════════════════════════════════════

from aiohttp import web

_running_bots: dict[str, BotSession] = {}  # bot_id → session

async def handle_send(request):
    """Agent Connector 调此接口发消息"""
    try:
        data = await request.json()
        bot_id = data.get("bot_id", "")
        to_user = data.get("to_user", "")
        text = data.get("text", "")
        ctx = data.get("context_token", "")

        bot = _running_bots.get(bot_id)
        if not bot:
            return web.json_response({"success": False, "error": "bot not found"}, status=404)

        await send_text(bot.token, to_user, text, ctx)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def handle_health(request):
    return web.json_response({"ok": True, "bots": len(_running_bots), "uptime": time.time()})

async def handle_welcome(request):
    """外部调此接口推送欢迎消息"""
    try:
        data = await request.json()
        bot_id = data.get("bot_id", "")
        to_user = data.get("to_user", "")
        nickname = data.get("nickname", "")
        msg = data.get("message", f"✨ 欢迎你，{nickname}！\n\n享客虾 Bot 已就绪 ✅\n\n试试对我说「你好」开始对话，或发送「帮助」了解我能做什么。")
        ctx = data.get("context_token", "")

        bot = _running_bots.get(bot_id)
        if not bot:
            return web.json_response({"success": False, "error": "bot not found"}, status=404)

        await send_text(bot.token, to_user, msg, ctx)
        bot.welcome_sent.add(to_user)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def handle_reload(request):
    try:
        bots = await load_bots()
        current_ids = {bid for bid, _ in bots}
        running_ids = set(_running_tasks.keys())
        new_count = 0
        for bot_id, token in bots:
            if bot_id not in running_ids:
                _bot_tokens[bot_id] = token
                t = asyncio.create_task(run_bot(bot_id, token, asyncio.Event()))
                _running_tasks[bot_id] = t
                root_log.info("New Bot (reload): %s", bot_id)
                new_count += 1
        for bot_id in running_ids - current_ids:
            _running_tasks[bot_id].cancel()
            del _running_tasks[bot_id]
            _bot_tokens.pop(bot_id, None)
            root_log.info("Bot stopped (reload): %s", bot_id)
        return web.json_response({"success": True, "new": new_count, "total": len(bots)})
    except Exception as e:
        root_log.error("reload error: %s", e)
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def handle_subscription_confirmed(request):
    """支付成功后，外部调此接口推送确认消息"""
    try:
        data = await request.json()
        bot_id = data.get("bot_id", "")
        to_user = data.get("to_user", "")
        nickname = data.get("nickname", "虾友")
        plan_name = data.get("plan_name", "会员")
        remain_days = data.get("remain_days", 30)

        bot = _running_bots.get(bot_id)
        if not bot:
            # 没 bot_id 时，按 user_id 找 Bot
            for bid, b in _running_bots.items():
                if b.user_id and b.user_id.split("@")[0] == to_user.split("@")[0]:
                    bot = b
                    bot_id = bid
                    break
            if not bot:
                return web.json_response({"success": False, "error": "bot not found"}, status=404)

        expires_at = data.get("expires_at", "")
        if expires_at:
            expiry_line = f"📅 到期：{expires_at}（剩余 {remain_days} 天）"
        else:
            expiry_line = f"📅 会员有效期 {remain_days} 天"
        msg = (
            f"🎉 开通成功！\n\n"
            f"🦞 {nickname}，欢迎成为享客虾 {plan_name} 伙伴！\n\n"
            f"{expiry_line}\n"
            f"✅ 解锁全部 AI 能力\n\n"
            f"回复「会员」查看会员状态\n"
            f""
        )
        ctx = data.get("context_token", "")
        await send_text(bot.token, to_user, msg, ctx)

        # 同时清除该用户的首次欢迎标记（防止后续再发一遍欢迎）
        if bot_id in _running_bots:
            _running_bots[bot_id].welcome_sent.discard(to_user)

        root_log.info(f"[订阅确认] Bot={bot_id} → {to_user[:20]} ({nickname})")
        return web.json_response({"success": True})
    except Exception as e:
        root_log.error(f"[订阅确认] 异常: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)
    """手动触发 Bot 重载（扫码激活后通知 keepalive 立即加载）"""
    try:
        bots = await load_bots()
        current_ids = {bid for bid, _ in bots}
        running_ids = set(_running_tasks.keys())

        new_count = 0
        # 新 Bot
        for bot_id, token in bots:
            if bot_id not in running_ids:
                _bot_tokens[bot_id] = token
                t = asyncio.create_task(run_bot(bot_id, token, asyncio.Event()))
                _running_tasks[bot_id] = t
                root_log.info("🆕 新 Bot（reload）: %s", bot_id)
                new_count += 1

        # 已停用
        for bot_id in running_ids - current_ids:
            _running_tasks[bot_id].cancel()
            del _running_tasks[bot_id]
            _bot_tokens.pop(bot_id, None)
            root_log.info("❌ Bot 已停用（reload）: %s", bot_id)

        return web.json_response({"success": True, "new": new_count, "total": len(bots)})
    except Exception as e:
        root_log.error("reload 异常: %s", e)
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def run_http_server():
    app = web.Application()
    app.router.add_post("/api/send", handle_send)
    app.router.add_post("/api/welcome", handle_welcome)
    app.router.add_post("/api/subscription-confirmed", handle_subscription_confirmed)
    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/api/reload", handle_reload)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HTTP_PORT)
    await site.start()
    root_log.info("🌐 HTTP API :9100 — send/welcome/health/reload")

# ══════════════════════════════════════════════
# DB 同步
# ══════════════════════════════════════════════

_running_tasks: dict[str, asyncio.Task] = {}
_bot_tokens: dict[str, str] = {}

async def sync_bots(shutdown: asyncio.Event):
    while not shutdown.is_set():
        try:
            bots = await load_bots()
            current_ids = {bid for bid, _ in bots}
            running_ids = set(_running_tasks.keys())

            # 新 Bot
            for bot_id, token in bots:
                if bot_id not in running_ids:
                    root_log.info("🆕 新 Bot: %s", bot_id)
                    _bot_tokens[bot_id] = token
                    t = asyncio.create_task(run_bot(bot_id, token, shutdown))
                    _running_tasks[bot_id] = t

            # Token 变更
            for bot_id, token in bots:
                old = _bot_tokens.get(bot_id)
                if old and old != token and bot_id in _running_tasks:
                    root_log.info("🔄 Token 变更，重启: %s", bot_id)
                    clear_pause(bot_id)
                    _running_tasks[bot_id].cancel()
                    _bot_tokens[bot_id] = token
                    await asyncio.sleep(2)
                    t = asyncio.create_task(run_bot(bot_id, token, shutdown))
                    _running_tasks[bot_id] = t

            # 已停用
            for bot_id in running_ids - current_ids:
                root_log.info("❌ Bot 已停用: %s", bot_id)
                _running_tasks[bot_id].cancel()
                del _running_tasks[bot_id]
                _bot_tokens.pop(bot_id, None)

            # 清理已完成
            for bot_id in list(_running_tasks.keys()):
                if _running_tasks[bot_id].done():
                    try:
                        _running_tasks[bot_id].result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        root_log.warning("Bot %s 异常退出: %s", bot_id, e)
                    del _running_tasks[bot_id]
                    _bot_tokens.pop(bot_id, None)

            # 同步 _running_bots (给 HTTP API 用)
            for bot_id, task in _running_tasks.items():
                if not task.done() and bot_id not in _running_bots:
                    # 还没跑起来，等等
                    pass
        except Exception as e:
            root_log.error("sync_bots 异常: %s", e)

        try:
            await asyncio.wait_for(asyncio.get_event_loop().create_future(), timeout=DB_RELOAD_INTERVAL)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break

# Bot session 注册 (run_bot 里调用)
def register_bot(bot_id: str, bot: BotSession):
    _running_bots[bot_id] = bot

def unregister_bot(bot_id: str):
    _running_bots.pop(bot_id, None)

# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

async def main():
    root_log.info("=" * 50)
    root_log.info("Keepalive Service 启动")
    root_log.info("  保活层 :9100（暗号秒回 + iLink 长轮询）")
    root_log.info("  Agent 层 :9101（AI 推理服务）")
    root_log.info("  DB 刷新: %ds", DB_RELOAD_INTERVAL)
    root_log.info("=" * 50)

    _ensure_code_file()

    shutdown = asyncio.Event()

    # 信号处理
    loop = asyncio.get_event_loop()
    sig_count = 0
    def _signal():
        nonlocal sig_count
        sig_count += 1
        if sig_count >= 2:
            root_log.warning("二次信号，强制退出")
            sys.exit(1)
        root_log.info("📴 graceful shutdown...")
        shutdown.set()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _signal)
        except NotImplementedError:
            pass

    # 启动 HTTP API
    await run_http_server()

    # 首次加载 Bot
    bots = await load_bots()
    root_log.info("首次加载 %d 个 Bot:", len(bots))
    for bot_id, token in bots:
        _bot_tokens[bot_id] = token
        t = asyncio.create_task(run_bot(bot_id, token, shutdown))
        _running_tasks[bot_id] = t
        root_log.info("  ✅ %s", bot_id)

    # 后台 DB 同步
    sync_task = asyncio.create_task(sync_bots(shutdown))

    # 加载已欢迎用户，防止重启重复欢迎
    try:
        import asyncpg
        _wl_db = await asyncpg.connect(DB_DSN)
        try:
            _rr = await _wl_db.fetch("SELECT channel_user_id FROM channel_bindings WHERE welcomed = true")
            for _rw in _rr:
                for _bid, _bt in _running_bots.items():
                    _bt.welcome_sent.add(_rw["channel_user_id"])
            root_log.info("加载 %d 个已欢迎用户", len(_rr))
        finally:
            await _wl_db.close()
    except Exception as _we:
        root_log.warning("加载已欢迎用户失败: %s", _we)
    root_log.info("✅ 全部就绪，等待消息...")
    await shutdown.wait()

    root_log.info("关闭中...")
    for task in list(_running_tasks.values()):
        task.cancel()
    sync_task.cancel()
    if _running_tasks or sync_task:
        await asyncio.gather(*_running_tasks.values(), sync_task, return_exceptions=True)
    root_log.info("Keepalive Service 已停止")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
